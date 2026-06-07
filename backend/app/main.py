
from contextlib import asynccontextmanager
import json
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
import shutil
from typing import List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, case
from sqlalchemy.orm import Session, aliased
from pathlib import Path
from backend.app.cloudinary_backup import backup_mgr
from starlette.middleware.sessions import SessionMiddleware
from fastapi import Request
from fastapi.responses import RedirectResponse
import secrets
import backend.app.auth
from backend.app.auth import login_required, has_perm, pwd_context
from backend.app.db import SessionLocal, User

from .db import SessionLocal, engine, ensure_schema
from .models import (
    Base,
    Doctor,
    DoctorCommissionRule,
    DoctorTransaction,
    InventoryMove,
    Item,
    ItemCategory,
    ItemUnit,
    ItemLot,
    StockOpening,
    StockOpeningLine,
    DamageNote,
    DamageLine,
    EmployeeAddition,
    EmployeeDeduction,
    EmployeeSalary,
    CashAccount,
    CashTransaction,
    OtherExpense,
    SalesInvoicePayment,
    PurchaseInvoicePayment,
    Location,
    LocationTransaction,
    Purchase,
    PurchaseItem,
    PurchaseOrder,
    PurchaseOrderLine,
    Representative,
    Stocktake,
    StocktakeLine,
    Supplier,
    SupplierAdjustment,
    SupplierTransaction,
    SalesOrder,
    SalesOrderLine,
    Transfer,
    TransferAllocation,
    TransferLine,
)
from .repo import (
    backup_db,
    ensure_backups_dir,
    get_db,
    list_backups,
    load_settings,
    restore_db,
    save_settings,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    ensure_backups_dir()
    with SessionLocal() as db:
        ensure_cash_accounts(db)
        ensure_supplier_transactions(db)
        ensure_purchase_shipping_expenses(db)
        backend.app.auth._ensure_default_admin(db)
    yield

from backend.app.auth import (
    authenticate,
    login_user,
    logout_user,
    get_current_user,
    login_required,
)

app = FastAPI(lifespan=lifespan)

secretKey = secrets.token_urlsafe(64)
# Session cookies
app.add_middleware(
    SessionMiddleware,
    secret_key=secretKey,
    session_cookie="erp_session",
    same_site="lax",
    https_only=False,
    max_age=60*60*8,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.auto_reload = True
templates.env.cache = {}

#---------------------------------#
#Seccion Login
#---------------------------------#

SESSION_KEY = "user"

def get_current_user(request: Request):
    username = request.session.get(SESSION_KEY)
    if not username:
        return None
    return {"username": username}

def require_user(request: Request):
    user = get_current_user(request)
    if not user:
        next_url = request.url.path
        if request.url.query:
            next_url += "?" + request.url.query
        raise HTTPException(status_code=302, headers={"Location": f"/login?next={next_url}"})
    return user
#-----------------------------------------------------------#

@app.get("/company-logo")
async def company_logo():
    settings = load_settings()
    logo_file = resolve_logo_file(settings.get("logo_path"))
    if not logo_file:
        return Response(status_code=404)
    return FileResponse(logo_file)


def clean_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def has_broken_text(value: Optional[str]) -> bool:
    if not value:
        return False
    if "\ufffd" in value:
        return True
    return "??" in value


def parse_decimal(value: Optional[str], default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    cleaned = value.strip()
    if cleaned == "":
        return default
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return default


def parse_date(value: Optional[str]) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return datetime.now()


def parse_date_end(value: Optional[str]) -> datetime:
    dt = parse_date(value)
    if dt.time() == datetime.min.time():
        return dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt


def parse_date_only(value: Optional[str]):
    if not value:
        return datetime.now().date()
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return datetime.now().date()


def parse_month_year(value: Optional[str]) -> Optional[date]:
    cleaned = clean_text(value)
    if not cleaned:
        return None
    normalized = cleaned.replace("/", "-")
    parts = [p for p in normalized.split("-") if p]
    if len(parts) < 2:
        return None
    try:
        if len(parts[0]) == 4:
            year_val = int(parts[0])
            month_val = int(parts[1])
        elif len(parts[1]) == 4:
            month_val = int(parts[0])
            year_val = int(parts[1])
        else:
            return None
        return date(year_val, month_val, 1)
    except (TypeError, ValueError):
        return None


def normalize_logo_path(value: Optional[str]) -> str:
    if not value:
        return ""
    cleaned = value.strip()
    if not cleaned:
        return ""
    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://", "file://")):
        return cleaned
    if len(cleaned) >= 2 and cleaned[1] == ":":
        return "file:///" + cleaned.replace("\\", "/")
    if cleaned.startswith("\\\\"):
        return "file:///" + cleaned.replace("\\", "/")
    return cleaned


def get_print_settings():
    settings = load_settings()
    logo_path = settings.get("logo_path") or ""
    normalized = normalize_logo_path(logo_path)
    if normalized.lower().startswith(("http://", "https://")):
        settings["logo_url"] = normalized
    elif normalized:
        settings["logo_url"] = "/company-logo"
    else:
        settings["logo_url"] = ""
    return settings


def resolve_logo_file(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    candidate = Path(cleaned)
    if not candidate.is_absolute():
        candidate = (BASE_DIR / candidate).resolve()
    return candidate if candidate.exists() else None


def get_supplier_balance(
    db: Session,
    supplier_id: Optional[int],
    up_to: Optional[datetime] = None,
    exclude_purchase_id: Optional[int] = None,
) -> Decimal:
    if not supplier_id:
        return Decimal("0")

    tx_query = db.query(func.coalesce(func.sum(SupplierTransaction.amount), 0)).filter(
        SupplierTransaction.supplier_id == supplier_id
    )
    if up_to:
        tx_query = tx_query.filter(
            (SupplierTransaction.type == "opening_balance")
            | (SupplierTransaction.date <= up_to)
        )
    if exclude_purchase_id:
        tx_query = tx_query.filter(
            or_(
                SupplierTransaction.source_type.is_(None),
                SupplierTransaction.source_id.is_(None),
                ~SupplierTransaction.source_type.in_(["purchase", "purchase_return"]),
                SupplierTransaction.source_id != exclude_purchase_id,
            )
        )
    tx_total = tx_query.scalar()
    if tx_total is not None:
        return Decimal(str(tx_total or 0))

    purchases_q = db.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
        Purchase.supplier_id == supplier_id,
        Purchase.kind == "purchase",
    )
    returns_q = db.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
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
        db.query(func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
        .join(Purchase)
        .filter(Purchase.supplier_id == supplier_id)
    )
    if up_to:
        payments_q = payments_q.filter(PurchaseInvoicePayment.date <= up_to)
    payments_total = Decimal(str(payments_q.scalar() or 0))

    adjustments_q = db.query(
        SupplierAdjustment.adjustment_type,
        func.coalesce(func.sum(SupplierAdjustment.amount), 0),
    ).filter(SupplierAdjustment.supplier_id == supplier_id)
    if up_to:
        adjustments_q = adjustments_q.filter(
            (SupplierAdjustment.adjustment_type == "opening_balance")
            | (SupplierAdjustment.date <= up_to.date())
        )
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


def get_location_balance(
    db: Session,
    location_id: Optional[int],
    up_to: Optional[datetime] = None,
    exclude_sale_id: Optional[int] = None,
) -> Decimal:
    if not location_id:
        return Decimal("0")
    query = db.query(func.coalesce(func.sum(LocationTransaction.amount), 0)).filter(
        LocationTransaction.location_id == location_id
    )
    if up_to:
        query = query.filter(
            (LocationTransaction.type == "opening_balance")
            | (LocationTransaction.date <= up_to)
        )
    if exclude_sale_id:
        query = query.filter(
            or_(
                LocationTransaction.source_type.is_(None),
                LocationTransaction.source_id.is_(None),
                LocationTransaction.source_type != "sale",
                LocationTransaction.source_id != exclude_sale_id,
            )
        )
    value = query.scalar() or 0
    return Decimal(str(value))


def _supplier_tx_amount(adjustment_type: str, amount: Decimal) -> Decimal:
    if adjustment_type in {"addition", "opening_balance"}:
        return amount
    if adjustment_type == "discount":
        return Decimal("0") - amount
    return amount


def rebuild_supplier_ledger_for_supplier(db: Session, supplier_id: int) -> None:
    transactions = (
        db.query(SupplierTransaction)
        .filter(SupplierTransaction.supplier_id == supplier_id)
        .order_by(SupplierTransaction.date.asc(), SupplierTransaction.id.asc())
        .all()
    )
    balance = Decimal("0")
    for tx in transactions:
        tx.prev_balance_snapshot = balance
        balance += Decimal(str(tx.amount or 0))
        tx.new_balance_snapshot = balance
    db.commit()


def add_supplier_transaction(
    db: Session,
    supplier_id: int,
    date: datetime,
    amount: Decimal,
    tx_type: str,
    notes: Optional[str] = None,
    source_type: Optional[str] = None,
    source_id: Optional[int] = None,
) -> None:
    db.add(
        SupplierTransaction(
            date=date,
            supplier_id=supplier_id,
            type=tx_type,
            amount=amount,
            notes=clean_text(notes),
            source_type=source_type,
            source_id=source_id,
        )
    )


def ensure_supplier_transactions(db: Session) -> None:
    existing = db.query(SupplierTransaction.id).first()
    if existing:
        return

    entries = []
    purchases = db.query(Purchase).all()
    for p in purchases:
        if not p.supplier_id:
            continue
        amt = Decimal(str(p.total or 0))
        if p.kind == "purchase_return":
            amt = Decimal("0") - amt
            tx_type = "purchase_return"
        else:
            tx_type = "purchase"
        entries.append(
            {
                "supplier_id": p.supplier_id,
                "date": p.date,
                "amount": amt,
                "type": tx_type,
                "notes": "فاتورة شراء" if tx_type == "purchase" else "مرتجع شراء",
                "source_type": tx_type,
                "source_id": p.id,
            }
        )

    payments = db.query(PurchaseInvoicePayment).all()
    for pay in payments:
        purchase = db.query(Purchase).filter(Purchase.id == pay.purchase_id).first()
        if not purchase or not purchase.supplier_id:
            continue
        entries.append(
            {
                "supplier_id": purchase.supplier_id,
                "date": pay.date,
                "amount": Decimal("0") - Decimal(str(pay.amount or 0)),
                "type": "payment",
                "notes": pay.notes or "سداد فاتورة شراء",
                "source_type": "purchase_payment",
                "source_id": pay.id,
            }
        )

    adjustments = db.query(SupplierAdjustment).all()
    for adj in adjustments:
        entries.append(
            {
                "supplier_id": adj.supplier_id,
                "date": datetime.combine(adj.date, datetime.min.time()),
                "amount": _supplier_tx_amount(adj.adjustment_type, Decimal(str(adj.amount or 0))),
                "type": adj.adjustment_type,
                "notes": adj.notes,
                "source_type": "supplier_adjustment",
                "source_id": adj.id,
            }
        )

    priority = {
        "opening_balance": 0,
        "purchase": 1,
        "purchase_return": 2,
        "payment": 3,
        "addition": 4,
        "discount": 5,
    }
    entries.sort(
        key=lambda e: (
            e["supplier_id"],
            e["date"] or datetime.min,
            priority.get(e["type"], 9),
            e["source_id"] or 0,
        )
    )

    current_supplier = None
    balance = Decimal("0")
    for entry in entries:
        if current_supplier != entry["supplier_id"]:
            current_supplier = entry["supplier_id"]
            balance = Decimal("0")
        prev = balance
        balance = balance + entry["amount"]
        db.add(
            SupplierTransaction(
                date=entry["date"],
                supplier_id=entry["supplier_id"],
                type=entry["type"],
                amount=entry["amount"],
                prev_balance_snapshot=prev,
                new_balance_snapshot=balance,
                notes=entry["notes"],
                source_type=entry["source_type"],
                source_id=entry["source_id"],
            )
        )
    db.commit()


def upsert_purchase_supplier_tx(db: Session, purchase: Purchase) -> None:
    if not purchase or not purchase.supplier_id:
        return
    tx_type = "purchase_return" if purchase.kind == "purchase_return" else "purchase"
    amount = Decimal(str(purchase.total or 0))
    if tx_type == "purchase_return":
        amount = Decimal("0") - amount
    tx = (
        db.query(SupplierTransaction)
        .filter(
            SupplierTransaction.source_type.in_(["purchase", "purchase_return"]),
            SupplierTransaction.source_id == purchase.id,
        )
        .first()
    )
    if tx:
        tx.date = purchase.date
        tx.type = tx_type
        tx.amount = amount
        tx.notes = "فاتورة شراء" if tx_type == "purchase" else "مرتجع شراء"
        tx.source_type = tx_type
    else:
        add_supplier_transaction(
            db,
            supplier_id=purchase.supplier_id,
            date=purchase.date,
            amount=amount,
            tx_type=tx_type,
            notes="فاتورة شراء" if tx_type == "purchase" else "مرتجع شراء",
            source_type=tx_type,
            source_id=purchase.id,
        )
        db.flush()
    rebuild_supplier_ledger_for_supplier(db, purchase.supplier_id)


def ensure_cash_accounts(db: Session) -> None:
    main = db.query(CashAccount).filter(CashAccount.is_main == True).first()
    if not main:
        db.add(CashAccount(name="الخزنة الرئيسية", is_main=True))
        db.commit()
    reps = db.query(Representative).all()
    for rep in reps:
        existing = db.query(CashAccount).filter(CashAccount.rep_id == rep.id).first()
        if not existing:
            db.add(CashAccount(name=f"خزنة {rep.name}", rep_id=rep.id, is_main=False))
    db.commit()


def get_main_cash_account(db: Session) -> CashAccount:
    main = db.query(CashAccount).filter(CashAccount.is_main == True).first()
    if not main:
        main = CashAccount(name="الخزنة الرئيسية", is_main=True)
        db.add(main)
        db.commit()
        db.refresh(main)
    return main


def get_rep_cash_account(db: Session, rep_id: int) -> CashAccount:
    account = db.query(CashAccount).filter(CashAccount.rep_id == rep_id).first()
    if not account:
        rep = db.query(Representative).filter(Representative.id == rep_id).first()
        account = CashAccount(name=f"خزنة {rep.name if rep else rep_id}", rep_id=rep_id, is_main=False)
        db.add(account)
        db.commit()
        db.refresh(account)
    return account


def get_location_default_rep_id(location: Optional[Location]) -> Optional[int]:
    if not location:
        return None
    if location.rep_id:
        return int(location.rep_id)
    if location.reps:
        ordered = sorted((r for r in location.reps if r and r.id), key=lambda r: r.id)
        if ordered:
            return int(ordered[0].id)
    return None


def get_customer_default_cash_account(db: Session, customer: Optional[Location]) -> CashAccount:
    rep_id = get_location_default_rep_id(customer)
    if rep_id:
        return get_rep_cash_account(db, rep_id)
    return get_main_cash_account(db)


def get_cash_balance(db: Session, account_id: int) -> Decimal:
    incoming = (
        db.query(func.coalesce(func.sum(CashTransaction.amount), 0))
        .filter(CashTransaction.to_account_id == account_id)
        .scalar()
    )
    outgoing = (
        db.query(func.coalesce(func.sum(CashTransaction.amount), 0))
        .filter(CashTransaction.from_account_id == account_id)
        .scalar()
    )
    return Decimal(str(incoming or 0)) - Decimal(str(outgoing or 0))


def get_cash_opening_balance(db: Session, account_id: int) -> Decimal:
    incoming = (
        db.query(func.coalesce(func.sum(CashTransaction.amount), 0))
        .filter(CashTransaction.to_account_id == account_id)
        .filter(CashTransaction.type == "opening_balance")
        .scalar()
    )
    outgoing = (
        db.query(func.coalesce(func.sum(CashTransaction.amount), 0))
        .filter(CashTransaction.from_account_id == account_id)
        .filter(CashTransaction.type == "opening_balance")
        .scalar()
    )
    return Decimal(str(incoming or 0)) - Decimal(str(outgoing or 0))


def sync_purchase_shipping_expense(db: Session, purchase: Optional[Purchase]) -> None:
    if not purchase:
        return
    db.query(OtherExpense).filter(
        OtherExpense.source_type == "purchase_shipping",
        OtherExpense.source_id == purchase.id,
    ).delete(synchronize_session=False)
    db.query(CashTransaction).filter(
        CashTransaction.source_type == "purchase_shipping",
        CashTransaction.source_id == purchase.id,
    ).delete(synchronize_session=False)

    shipping_cost = Decimal(str(purchase.shipping_cost or 0))
    if shipping_cost <= 0:
        return
    main_cash = get_main_cash_account(db)
    notes = f"تكلفة شحن للمشتريات رقم {purchase.id}"
    db.add(
        OtherExpense(
            date=purchase.date,
            title="مصروف شحن",
            amount=shipping_cost,
            notes=notes,
            source_type="purchase_shipping",
            source_id=purchase.id,
        )
    )
    db.add(
        CashTransaction(
            date=purchase.date,
            type="other_expense",
            amount=shipping_cost,
            from_account_id=main_cash.id,
            to_account_id=None,
            notes=notes,
            source_type="purchase_shipping",
            source_id=purchase.id,
        )
    )


def ensure_purchase_shipping_expenses(db: Session) -> None:
    purchases = db.query(Purchase).filter(Purchase.shipping_cost > 0).all()
    for purchase in purchases:
        exp_exists = (
            db.query(OtherExpense)
            .filter(
                OtherExpense.source_type == "purchase_shipping",
                OtherExpense.source_id == purchase.id,
            )
            .first()
        )
        cash_exists = (
            db.query(CashTransaction)
            .filter(
                CashTransaction.source_type == "purchase_shipping",
                CashTransaction.source_id == purchase.id,
            )
            .first()
        )
        if not exp_exists or not cash_exists:
            sync_purchase_shipping_expense(db, purchase)
    db.commit()


def get_main_warehouse(db: Session) -> Location:
    warehouse = (
        db.query(Location)
        .filter(Location.type == "warehouse", Location.name == "المخزن الرئيسي")
        .first()
    )
    if not warehouse:
        warehouse = Location(name="المخزن الرئيسي", type="warehouse")
        db.add(warehouse)
        db.commit()
        db.refresh(warehouse)
    return warehouse


def build_stock_map(db: Session, location_id: int):
    rows = (
        db.query(
            InventoryMove.item_id,
            func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0).label("balance"),
        )
        .filter(InventoryMove.location_id == location_id)
        .group_by(InventoryMove.item_id)
        .all()
    )
    return {row.item_id: row.balance for row in rows}


def get_available_lots(db: Session, location_id: int, item_id: int):
    rows = (
        db.query(
            InventoryMove.lot_id,
            func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0).label("balance"),
        )
        .filter(
            InventoryMove.location_id == location_id,
            InventoryMove.item_id == item_id,
            InventoryMove.lot_id.isnot(None),
        )
        .group_by(InventoryMove.lot_id)
        .all()
    )
    balances = {row.lot_id: Decimal(str(row.balance or 0)) for row in rows}
    lots = (
        db.query(ItemLot)
        .filter(ItemLot.id.in_(list(balances.keys())))
        .order_by(ItemLot.created_at.asc())
        .all()
    )
    return [(lot, balances.get(lot.id, Decimal("0"))) for lot in lots if balances.get(lot.id, Decimal("0")) > 0]


def get_or_create_lot(
    db: Session,
    item_id: int,
    lot_code: str,
    purchase_price: Optional[Decimal] = None,
    expiry_date: Optional[date] = None,
):
    lot = (
        db.query(ItemLot)
        .filter(ItemLot.item_id == item_id, ItemLot.lot_code == lot_code)
        .first()
    )
    if not lot:
        lot = ItemLot(
            item_id=item_id,
            lot_code=lot_code,
            purchase_price=purchase_price,
            expiry_date=expiry_date,
        )
        db.add(lot)
        db.flush()
        return lot
    if expiry_date:
        lot.expiry_date = expiry_date
    return lot


def get_lot_balance(db: Session, location_id: int, lot_id: int) -> Decimal:
    balance = (
        db.query(func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0))
        .filter(InventoryMove.location_id == location_id, InventoryMove.lot_id == lot_id)
        .scalar()
        or 0
    )
    return Decimal(str(balance))


def find_commission_rule(db: Session, doctor_id: int, location_id: int, item_id: int, date_val):
    return (
        db.query(DoctorCommissionRule)
        .filter(
            DoctorCommissionRule.doctor_id == doctor_id,
            DoctorCommissionRule.pharmacy_location_id == location_id,
            DoctorCommissionRule.item_id == item_id,
            DoctorCommissionRule.active.is_(True),
            or_(DoctorCommissionRule.start_date.is_(None), DoctorCommissionRule.start_date <= date_val),
            or_(DoctorCommissionRule.end_date.is_(None), DoctorCommissionRule.end_date >= date_val),
        )
        .first()
    )

#---------------------------------#
#Seccion Auth
#---------------------------------#

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/account/settings", error: str | None = None):
    return templates.TemplateResponse(
        "login/login.html",
        {
            "request": request,
            "next": next,
            "error": error,
            "user": get_current_user(request),
            "settings": get_print_settings(),
        },
    )

@app.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/account/settings"),
):
    user = authenticate(username, password)
    if not user:
        return RedirectResponse(url=f"/login?next={next}&error=بيانات الدخول غير صحيحة", status_code=302)
    login_user(request, user["username"])
    return RedirectResponse(url=next or "/login/settings", status_code=302)

@app.post("/logout")
def logout(request: Request):
    logout_user(request)
    return RedirectResponse("/login", status_code=302)

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

@app.get("/account/settings", response_class=HTMLResponse)
def account_settings(request: Request, db: Session = Depends(get_db)):
    guard = login_required(request)
    if isinstance(guard, RedirectResponse):
        return guard

    me = guard

    can_manage_users = has_perm(me, "users.manage")

    users = []
    if can_manage_users:
        users = db.query(User).order_by(User.id.desc()).all()
        for u in users:
            perms = u.permissions or "[]"
            perms_clean = (
                perms.replace("[", "")
                     .replace("]", "")
                     .replace('"', "")
                     .replace("'", "")
            )
            u.permissions_list = [x.strip() for x in perms_clean.split(",") if x.strip()]

    return templates.TemplateResponse(
        "login/account_settings.html",
        {
            "request": request,
            "user": me,
            "me": me,
            "users": users,
            "can_manage_users": can_manage_users,
        },
    )

@app.post("/account/settings")
def save_my_settings(
    request: Request,
    full_name: str = Form(""),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    guard = login_required(request)
    if isinstance(guard, RedirectResponse):
        return guard
    user = guard

    with SessionLocal() as db:
        me = db.query(User).filter(User.username == user["username"]).first()
        if not me:
            return RedirectResponse("/login", status_code=302)

        me.full_name = (full_name or "").strip()

        if new_password.strip():
            if not current_password.strip() or not pwd_context.verify(current_password, me.password_hash):
                return RedirectResponse("/account/settings?err=wrong_password", status_code=302)

            if new_password != confirm_password:
                return RedirectResponse("/account/settings?err=password_mismatch", status_code=302)

            me.password_hash = pwd_context.hash(new_password)

        db.commit()

    return RedirectResponse("/account/settings?ok=1", status_code=302)

@app.post("/admin/users/create")
def admin_create_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(""),
    password: str = Form(...),
    is_admin: str = Form("0"),
    permissions: str = Form(""),
):
    guard = login_required(request)
    if isinstance(guard, RedirectResponse):
        return guard
    user = guard

    if not has_perm(user, "users.manage"):
        return RedirectResponse("/account/settings?err=no_perm", status_code=302)

    username = username.strip()
    full_name = full_name.strip()

    # permissions input: "items.read,items.write,users.manage"
    perms_list = [p.strip() for p in permissions.split(",") if p.strip()]
    perms_json = json.dumps(perms_list)

    with SessionLocal() as db:
        exists = db.query(User).filter(User.username == username).first()
        if exists:
            return RedirectResponse("/account/settings?err=user_exists", status_code=302)

        u = User(
            username=username,
            full_name=full_name,
            password_hash=pwd_context.hash(password),
            is_admin=(is_admin == "1"),
            permissions=perms_json,
            is_active=True,
        )
        db.add(u)
        db.commit()

    return RedirectResponse("/account/settings?ok=user_created", status_code=302)

@app.post("/admin/users/{user_id}/update")
def admin_update_user(
    request: Request,
    user_id: int,
    full_name: str = Form(""),
    is_admin: str = Form("0"),
    is_active: str = Form("1"),
    permissions: str = Form(""),
):
    guard = login_required(request)
    if isinstance(guard, RedirectResponse):
        return guard
    user = guard

    if not has_perm(user, "users.manage"):
        return RedirectResponse("/account/settings?err=no_perm", status_code=302)

    perms_list = [p.strip() for p in permissions.split(",") if p.strip()]
    perms_json = json.dumps(perms_list)

    with SessionLocal() as db:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return RedirectResponse("/account/settings?err=not_found", status_code=302)

        u.full_name = (full_name or "").strip()
        u.is_admin = (is_admin == "1")
        u.is_active = (is_active == "1")
        u.permissions = perms_json
        db.commit()

    return RedirectResponse("/account/settings?ok=user_updated", status_code=302)

@app.post("/admin/users/{user_id}/password")
def admin_reset_password(
    request: Request,
    user_id: int,
    new_password: str = Form(...),
):
    guard = login_required(request)
    if isinstance(guard, RedirectResponse):
        return guard
    user = guard

    if not has_perm(user, "users.manage"):
        return RedirectResponse("/account/settings?err=no_perm", status_code=302)

    with SessionLocal() as db:
        u = db.query(User).filter(User.id == user_id).first()
        if not u:
            return RedirectResponse("/account/settings?err=not_found", status_code=302)
        u.password_hash = pwd_context.hash(new_password)
        db.commit()

    return RedirectResponse("/account/settings?ok=pwd_reset", status_code=302)

# -------------------------
# Items
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request,db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/items")

@app.get("/items", response_class=HTMLResponse)
async def read_items(request: Request, q: Optional[str] = None, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    items = db.query(Item).order_by(Item.id.desc()).all()
    if q:
        like = f"%{q.strip()}%"
        items = (
            db.query(Item)
            .filter(Item.name.ilike(like))
            .order_by(Item.id.desc())
            .all()
        )
    main_wh = get_main_warehouse(db)
    stock_map = build_stock_map(db, main_wh.id)
    return templates.TemplateResponse(
        "items/page.html",
        {
            "request": request,
            "items": items,
            "active_page": "items",
            "q": q or "",
            "stock_map": stock_map,
        },
    )

@app.get("/items/table", response_class=HTMLResponse)
async def read_items_table(request: Request, q: Optional[str] = None, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    items = db.query(Item).order_by(Item.id.desc()).all()
    if q:
        like = f"%{q.strip()}%"
        items = (
            db.query(Item)
            .filter(Item.name.ilike(like))
            .order_by(Item.id.desc())
            .all()
        )
    main_wh = get_main_warehouse(db)
    stock_map = build_stock_map(db, main_wh.id)
    return templates.TemplateResponse(
        "items/table.html",
        {"request": request, "items": items, "stock_map": stock_map},
    )

@app.get("/items/section/{section}", response_class=HTMLResponse)
async def items_section(request: Request, section: str, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if section == "items":
        items = db.query(Item).order_by(Item.id.desc()).all()
        main_wh = get_main_warehouse(db)
        stock_map = build_stock_map(db, main_wh.id)
        return templates.TemplateResponse(
            "items/sections/items.html",
            {"request": request, "items": items, "q": "", "stock_map": stock_map},
        )
    if section == "item-units":
        units = db.query(ItemUnit).order_by(ItemUnit.id.desc()).all()
        items = db.query(Item).order_by(Item.name.asc()).all()
        return templates.TemplateResponse(
            "items/sections/item_units.html",
            {"request": request, "units": units, "items": items},
        )
    if section == "opening-stock":
        openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
        locations = db.query(Location).order_by(Location.name.asc()).all()
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
        return templates.TemplateResponse(
            "items/sections/opening_stock.html",
            {
                "request": request,
                "openings": openings,
                "locations": locations,
                "items": items_data,
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    if section == "damages":
        damages = db.query(DamageNote).order_by(DamageNote.id.desc()).all()
        locations = db.query(Location).order_by(Location.name.asc()).all()
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
        return templates.TemplateResponse(
            "items/sections/damages.html",
            {
                "request": request,
                "damages": damages,
                "locations": locations,
                "items": items_data,
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    if section == "locations":
        locations = db.query(Location).order_by(Location.id.desc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        return templates.TemplateResponse(
            "items/sections/locations.html",
            {
                "request": request,
                "locations": locations,
                "reps": reps,
                "transactions": [],
                "totals": {"balance": Decimal("0"), "total_debt": Decimal("0"), "total_paid": Decimal("0")},
                "opening_balance": Decimal("0"),
                "selected_entity_id": None,
                "start_date": "",
                "end_date": "",
            },
        )
    if section == "transfers":
        transfers = (
            db.query(Transfer)
            .filter(Transfer.kind == "transfer")
            .order_by(Transfer.id.desc())
            .all()
        )
        return templates.TemplateResponse(
            "items/sections/transfers.html",
            {"request": request, "transfers": transfers},
        )
    if section == "reports":
        items = db.query(Item).order_by(Item.id.desc()).all()
        return templates.TemplateResponse(
            "items/sections/reports.html",
            {"request": request, "items": items},
        )
    if section == "transfers-new":
        locations = db.query(Location).order_by(Location.name.asc()).all()
        items = db.query(Item).order_by(Item.name.asc()).all()
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        items_data = [
            {"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items
        ]
        doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
        return templates.TemplateResponse(
            "items/sections/transfers_new.html",
            {
                "request": request,
                "locations": locations,
                "items": items_data,
                "doctors": doctors_data,
                "reps": reps,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": [],
            },
        )
    return RedirectResponse(url="/items", status_code=303)

# -------------------------
# Customers (Locations + Transfers)
# -------------------------
@app.get("/customers", response_class=HTMLResponse)
async def customers_page(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    locations = db.query(Location).order_by(Location.id.desc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "customers/page.html",
        {
            "request": request,
            "locations": locations,
            "reps": reps,
            "transactions": [],
            "totals": {"balance": Decimal("0"), "total_debt": Decimal("0"), "total_paid": Decimal("0")},
            "opening_balance": Decimal("0"),
            "selected_entity_id": None,
            "start_date": "",
            "end_date": "",
            "active_page": "customers",
        },
    )

@app.get("/customers/section/{section}", response_class=HTMLResponse)
async def customers_section(request: Request, section: str, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if section == "locations":
        locations = db.query(Location).order_by(Location.id.desc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        return templates.TemplateResponse(
            "items/sections/locations.html",
            {
                "request": request,
                "locations": locations,
                "reps": reps,
                "transactions": [],
                "totals": {"balance": Decimal("0"), "total_debt": Decimal("0"), "total_paid": Decimal("0")},
                "opening_balance": Decimal("0"),
                "selected_entity_id": None,
                "start_date": "",
                "end_date": "",
            },
        )
    if section == "transfers":
        transfers = (
            db.query(Transfer)
            .filter(Transfer.kind == "transfer")
            .order_by(Transfer.id.desc())
            .all()
        )
        return templates.TemplateResponse(
            "customers/sections/transfers.html",
            {"request": request, "transfers": transfers},
        )
    if section == "transfers-new":
        locations = db.query(Location).order_by(Location.name.asc()).all()
        items = db.query(Item).order_by(Item.name.asc()).all()
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        items_data = [
            {"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items
        ]
        doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
        return templates.TemplateResponse(
            "customers/sections/transfers_new.html",
            {
                "request": request,
                "locations": locations,
                "items": items_data,
                "doctors": doctors_data,
                "reps": reps,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": [],
            },
        )
    if section == "opening-balance":
        locations = db.query(Location).order_by(Location.name.asc()).all()
        opening_rows = (
            db.query(LocationTransaction.location_id, func.coalesce(func.sum(LocationTransaction.amount), 0))
            .filter(LocationTransaction.type == "opening_balance")
            .group_by(LocationTransaction.location_id)
            .all()
        )
        opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
        return templates.TemplateResponse(
            "customers/sections/opening_balance.html",
            {
                "request": request,
                "locations": locations,
                "opening_map": opening_map,
                "transactions": [],
                "totals": {"balance": Decimal("0"), "total_debt": Decimal("0"), "total_paid": Decimal("0")},
                "opening_balance": Decimal("0"),
                "selected_entity_id": None,
                "start_date": "",
                "end_date": "",
                "message": "",
                "last_receipt_id": None,
            },
        )
    if section == "reports":
        return templates.TemplateResponse(
            "customers/sections/reports.html",
            {
                "request": request,
            },
        )
    return RedirectResponse(url="/customers", status_code=303)

@app.get("/items/report/{report}", response_class=HTMLResponse)
async def items_reports(
    request: Request,
    report: str,
    location_id: Optional[str] = None,
    item_id: Optional[str] = None,
    start_date: str = "",
    end_date: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if report == "items-list":
        items = db.query(Item).order_by(Item.id.desc()).all()
        return templates.TemplateResponse(
            "items/reports/items_list.html",
            {"request": request, "items": items},
        )
    if report == "stock-balances":
        locations = db.query(Location).order_by(Location.name.asc()).all()
        main_wh = get_main_warehouse(db)
        selected_id = int(location_id) if location_id else main_wh.id
        rows = (
            db.query(
                Item.id,
                Item.name,
                Item.purchase_price,
                Item.sale_price,
                func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0).label("balance"),
            )
            .outerjoin(
                InventoryMove,
                (InventoryMove.item_id == Item.id) & (InventoryMove.location_id == selected_id),
            )
            .group_by(Item.id)
            .order_by(Item.id.desc())
            .all()
        )
        return templates.TemplateResponse(
            "items/reports/stock_balances.html",
            {
                "request": request,
                "rows": rows,
                "locations": locations,
                "selected_location": selected_id,
            },
        )
    if report == "movement":
        items = db.query(Item).order_by(Item.name.asc()).all()
        locations = db.query(Location).order_by(Location.name.asc()).all()
        filters = []
        if item_id:
            filters.append(InventoryMove.item_id == int(item_id))
        if location_id:
            filters.append(InventoryMove.location_id == int(location_id))
        base_query = db.query(InventoryMove).filter(*filters)
        query = (
            db.query(InventoryMove, Item, Location)
            .join(Item, InventoryMove.item_id == Item.id)
            .join(Location, InventoryMove.location_id == Location.id)
            .filter(*filters)
        )
        start_dt = None
        end_dt = None
        if start_date:
            start_dt = parse_date(start_date)
            query = query.filter(InventoryMove.date >= start_dt)
        if end_date:
            end_dt = parse_date_end(end_date)
            query = query.filter(InventoryMove.date <= end_dt)
        rows = query.order_by(InventoryMove.date.desc()).limit(200).all()
        totals_query = base_query
        if start_dt:
            totals_query = totals_query.filter(InventoryMove.date >= start_dt)
        if end_dt:
            totals_query = totals_query.filter(InventoryMove.date <= end_dt)
        totals_row = totals_query.with_entities(
            func.coalesce(func.sum(InventoryMove.qty_in), 0).label("total_in"),
            func.coalesce(func.sum(InventoryMove.qty_out), 0).label("total_out"),
            func.count(InventoryMove.id).label("total_count"),
        ).first()
        total_in = Decimal(str(totals_row.total_in or 0))
        total_out = Decimal(str(totals_row.total_out or 0))
        total_count = int(totals_row.total_count or 0)

        opening_balance = Decimal("0")
        if start_dt:
            opening_value = (
                base_query.filter(InventoryMove.date < start_dt)
                .with_entities(
                    func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0)
                )
                .scalar()
            )
            opening_balance = Decimal(str(opening_value or 0))
        net_movement = total_in - total_out
        closing_balance = opening_balance + net_movement
        return templates.TemplateResponse(
            "items/reports/movement.html",
            {
                "request": request,
                "rows": rows,
                "items": items,
                "locations": locations,
                "selected_item": int(item_id) if item_id else 0,
                "selected_location": int(location_id) if location_id else 0,
                "start_date": start_date,
                "end_date": end_date,
                "total_in": total_in,
                "total_out": total_out,
                "total_count": total_count,
                "opening_balance": opening_balance,
                "net_movement": net_movement,
                "closing_balance": closing_balance,
            },
        )
    return Response(status_code=404)

@app.post("/items", response_class=HTMLResponse)
async def create_item(
    request: Request,
    name: str = Form(...),
    unit: str = Form(""),
    item_kind: str = Form("general"),
    purchase_price: str = Form("0"),
    sale_price: str = Form("0"),
    is_active: Optional[bool] = Form(False),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    clean_name = clean_text(name)
    if clean_name:
        item = Item(
            name=clean_name,
            unit=clean_text(unit) or "علبة",
            purchase_price=parse_decimal(purchase_price),
            sale_price=parse_decimal(sale_price),
            is_active=bool(is_active),
            notes=clean_text(notes),
            item_kind=item_kind or "general",
        )
        db.add(item)
        db.commit()
    items = db.query(Item).order_by(Item.id.desc()).all()
    main_wh = get_main_warehouse(db)
    stock_map = build_stock_map(db, main_wh.id)
    return templates.TemplateResponse(
        "items/table.html",
        {"request": request, "items": items, "stock_map": stock_map},
    )

@app.delete("/items/{item_id}", response_class=HTMLResponse)
async def delete_item(request: Request, item_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    item = db.query(Item).filter(Item.id == item_id).first()
    if item:
        db.delete(item)
        db.commit()
    items = db.query(Item).order_by(Item.id.desc()).all()
    main_wh = get_main_warehouse(db)
    stock_map = build_stock_map(db, main_wh.id)
    return templates.TemplateResponse(
        "items/table.html",
        {"request": request, "items": items, "stock_map": stock_map},
    )

@app.post("/items/{item_id}/update", response_class=HTMLResponse)
async def update_item(
    request: Request,
    item_id: int,
    name: str = Form(...),
    unit: str = Form(""),
    item_kind: str = Form("general"),
    sale_price: str = Form("0"),
    is_active: Optional[bool] = Form(False),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    item = db.query(Item).filter(Item.id == item_id).first()
    clean_name = clean_text(name)
    if item and clean_name:
        item.name = clean_name
        item.unit = clean_text(unit) or "علبة"
        item.sale_price = parse_decimal(sale_price)
        item.is_active = bool(is_active)
        item.notes = clean_text(notes)
        item.item_kind = item_kind or "general"
        db.commit()
    items = db.query(Item).order_by(Item.id.desc()).all()
    main_wh = get_main_warehouse(db)
    stock_map = build_stock_map(db, main_wh.id)
    return templates.TemplateResponse(
        "items/table.html",
        {"request": request, "items": items, "stock_map": stock_map},
    )

# -------------------------
# Item Units
# -------------------------
@app.get("/item-units", response_class=HTMLResponse)
async def read_item_units(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    units = db.query(ItemUnit).order_by(ItemUnit.id.desc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    return templates.TemplateResponse(
        "item_units/page.html",
        {"request": request, "units": units, "items": items, "active_page": "item_units"},
    )

@app.get("/item-units/table", response_class=HTMLResponse)
async def item_units_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    units = db.query(ItemUnit).order_by(ItemUnit.id.desc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    return templates.TemplateResponse(
        "item_units/table.html", {"request": request, "units": units, "items": items}
    )

@app.post("/item-units", response_class=HTMLResponse)
async def create_item_unit(
    request: Request,
    item_id: str = Form(""),
    name: str = Form(...),
    factor: str = Form("1"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    clean_name = clean_text(name)
    factor_val = parse_decimal(factor, Decimal("1"))
    if item_id and clean_name and factor_val > 0:
        db.add(
            ItemUnit(
                item_id=int(item_id),
                name=clean_name,
                factor=factor_val,
                notes=clean_text(notes),
            )
        )
        db.commit()
    units = db.query(ItemUnit).order_by(ItemUnit.id.desc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    return templates.TemplateResponse(
        "item_units/table.html", {"request": request, "units": units, "items": items}
    )

@app.post("/item-units/{unit_id}/update", response_class=HTMLResponse)
async def update_item_unit(
    request: Request,
    unit_id: int,
    item_id: str = Form(""),
    name: str = Form(...),
    factor: str = Form("1"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    unit = db.query(ItemUnit).filter(ItemUnit.id == unit_id).first()
    clean_name = clean_text(name)
    factor_val = parse_decimal(factor, Decimal("1"))
    if unit and item_id and clean_name and factor_val > 0:
        unit.item_id = int(item_id)
        unit.name = clean_name
        unit.factor = factor_val
        unit.notes = clean_text(notes)
        db.commit()
    units = db.query(ItemUnit).order_by(ItemUnit.id.desc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    return templates.TemplateResponse(
        "item_units/table.html", {"request": request, "units": units, "items": items}
    )

@app.delete("/item-units/{unit_id}", response_class=HTMLResponse)
async def delete_item_unit(request: Request, unit_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    unit = db.query(ItemUnit).filter(ItemUnit.id == unit_id).first()
    if unit:
        db.delete(unit)
        db.commit()
    units = db.query(ItemUnit).order_by(ItemUnit.id.desc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    return templates.TemplateResponse(
        "item_units/table.html", {"request": request, "units": units, "items": items}
    )

# -------------------------
# Opening Stock
# -------------------------
@app.get("/opening-stock", response_class=HTMLResponse)
async def read_opening_stock(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
    locations = db.query(Location).order_by(Location.name.asc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            "items/sections/opening_stock.html",
            {
                "request": request,
                "openings": openings,
                "locations": locations,
                "items": items_data,
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    return templates.TemplateResponse(
        "opening_stock/page.html",
        {
            "request": request,
            "openings": openings,
            "locations": locations,
            "items": items_data,
            "active_page": "opening_stock",
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )

@app.get("/opening-stock/table", response_class=HTMLResponse)
async def opening_stock_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
    return templates.TemplateResponse(
        "opening_stock/table.html", {"request": request, "openings": openings}
    )


def _prepare_opening_stock_lines(
    item_id: List[str],
    lot_code: List[str],
    expiry_month: List[str],
    qty: List[str],
    unit_price: List[str],
):
    prepared = []
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        code = clean_text(lot_code[idx] if idx < len(lot_code) else "")
        if not code:
            continue
        qty_val = parse_decimal(qty[idx] if idx < len(qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        expiry_val = parse_month_year(expiry_month[idx] if idx < len(expiry_month) else "")
        if qty_val <= 0:
            continue
        prepared.append((int(raw_item_id), code, expiry_val, qty_val, price_val))
    return prepared


def _build_opening_edit_lines(opening: StockOpening):
    rows = []
    for line in sorted(opening.lines or [], key=lambda x: x.id or 0):
        lot_obj = line.lot
        lot_code_val = lot_obj.lot_code if lot_obj and lot_obj.lot_code else (line.lot_code_snapshot or "")
        expiry_month_val = (
            lot_obj.expiry_date.strftime("%Y-%m")
            if lot_obj and lot_obj.expiry_date
            else ""
        )
        rows.append(
            {
                "item_id": line.item_id,
                "lot_code": lot_code_val,
                "expiry_month": expiry_month_val,
                "qty": float(line.qty or 0),
                "unit_price": float(line.unit_price or 0),
            }
        )
    return rows


def _render_opening_stock_edit(
    request: Request,
    db: Session,
    opening: StockOpening,
    message: Optional[str] = None,
):
    locations = db.query(Location).order_by(Location.name.asc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    return templates.TemplateResponse(
        "opening_stock/edit.html",
        {
            "request": request,
            "opening": opening,
            "locations": locations,
            "items": items_data,
            "existing_lines": _build_opening_edit_lines(opening),
            "message": message,
            "active_page": "opening_stock",
        },
    )


@app.post("/opening-stock", response_class=HTMLResponse)
async def create_opening_stock(
    request: Request,
    date: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    lot_code: List[str] = Form([]),
    expiry_month: List[str] = Form([]),
    qty: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if not location_id:
        openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
        return templates.TemplateResponse(
            "opening_stock/table.html",
            {"request": request, "openings": openings, "message": "يرجى إدخال بيانات الأصناف."},
        )

    prepared = _prepare_opening_stock_lines(
        item_id=item_id,
        lot_code=lot_code,
        expiry_month=expiry_month,
        qty=qty,
        unit_price=unit_price,
    )

    if not prepared:
        openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
        return templates.TemplateResponse(
            "opening_stock/table.html",
            {"request": request, "openings": openings, "message": "يرجى إدخال بيانات الأصناف."},
        )

    opening = StockOpening(
        date=parse_date(date),
        location_id=int(location_id),
        notes=clean_text(notes),
    )
    db.add(opening)
    db.flush()

    for item_id_val, code, expiry_val, qty_val, price_val in prepared:
        lot = get_or_create_lot(db, item_id_val, code, expiry_date=expiry_val)
        db.add(
            StockOpeningLine(
                opening_id=opening.id,
                item_id=item_id_val,
                lot_id=lot.id,
                lot_code_snapshot=lot.lot_code,
                qty=qty_val,
                unit_price=price_val,
                discount_percent=Decimal("0"),
                discount_amount=Decimal("0"),
                line_total=(qty_val * price_val),
            )
        )
        db.add(
            InventoryMove(
                date=opening.date,
                location_id=int(location_id),
                item_id=item_id_val,
                lot_id=lot.id,
                qty_in=qty_val,
                qty_out=Decimal("0"),
                source_type="opening",
                source_id=opening.id,
                notes="رصيد أول المدة",
            )
        )

    db.commit()
    openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
    return templates.TemplateResponse(
        "opening_stock/table.html", {"request": request, "openings": openings}
    )

@app.get("/opening-stock/{opening_id}/edit", response_class=HTMLResponse)
async def edit_opening_stock(
    request: Request,
    opening_id: int,
    db: Session = Depends(get_db),
    user = Depends(require_user),
):
    if isinstance(user, RedirectResponse):
        return user
    opening = db.query(StockOpening).filter(StockOpening.id == opening_id).first()
    if not opening:
        return RedirectResponse(url="/opening-stock", status_code=303)
    return _render_opening_stock_edit(request, db, opening)


@app.post("/opening-stock/{opening_id}/update", response_class=HTMLResponse)
async def update_opening_stock(
    request: Request,
    opening_id: int,
    date: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    lot_code: List[str] = Form([]),
    expiry_month: List[str] = Form([]),
    qty: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user),
):
    if isinstance(user, RedirectResponse):
        return user
    opening = db.query(StockOpening).filter(StockOpening.id == opening_id).first()
    if not opening:
        return RedirectResponse(url="/opening-stock", status_code=303)

    if not location_id:
        return _render_opening_stock_edit(request, db, opening, "يرجى اختيار المخزن.")

    prepared = _prepare_opening_stock_lines(
        item_id=item_id,
        lot_code=lot_code,
        expiry_month=expiry_month,
        qty=qty,
        unit_price=unit_price,
    )
    if not prepared:
        return _render_opening_stock_edit(request, db, opening, "يرجى إدخال بيانات الأصناف.")

    opening.date = parse_date(date)
    opening.location_id = int(location_id)
    opening.notes = clean_text(notes)

    db.query(InventoryMove).filter(
        InventoryMove.source_type == "opening",
        InventoryMove.source_id == opening.id,
    ).delete(synchronize_session=False)
    db.query(StockOpeningLine).filter(
        StockOpeningLine.opening_id == opening.id,
    ).delete(synchronize_session=False)

    for item_id_val, code, expiry_val, qty_val, price_val in prepared:
        lot = get_or_create_lot(db, item_id_val, code, expiry_date=expiry_val)
        db.add(
            StockOpeningLine(
                opening_id=opening.id,
                item_id=item_id_val,
                lot_id=lot.id,
                lot_code_snapshot=lot.lot_code,
                qty=qty_val,
                unit_price=price_val,
                discount_percent=Decimal("0"),
                discount_amount=Decimal("0"),
                line_total=(qty_val * price_val),
            )
        )
        db.add(
            InventoryMove(
                date=opening.date,
                location_id=opening.location_id,
                item_id=item_id_val,
                lot_id=lot.id,
                qty_in=qty_val,
                qty_out=Decimal("0"),
                source_type="opening",
                source_id=opening.id,
                notes="رصيد أول المدة",
            )
        )

    db.commit()

    if request.headers.get("HX-Request") == "true":
        openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
        return templates.TemplateResponse(
            "opening_stock/table.html",
            {"request": request, "openings": openings, "message": "تم تعديل الرصيد بنجاح."},
        )
    return RedirectResponse(url="/opening-stock", status_code=303)


@app.delete("/opening-stock/{opening_id}", response_class=HTMLResponse)
async def delete_opening_stock(request: Request, opening_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    opening = db.query(StockOpening).filter(StockOpening.id == opening_id).first()
    if opening:
        db.query(InventoryMove).filter(
            InventoryMove.source_type == "opening",
            InventoryMove.source_id == opening.id,
        ).delete()
        db.delete(opening)
        db.commit()
    openings = db.query(StockOpening).order_by(StockOpening.id.desc()).all()
    return templates.TemplateResponse(
        "opening_stock/table.html", {"request": request, "openings": openings}
    )

# -------------------------
# Damage Notes
# -------------------------
@app.get("/damages", response_class=HTMLResponse)
async def read_damages(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    damages = db.query(DamageNote).order_by(DamageNote.id.desc()).all()
    locations = db.query(Location).order_by(Location.name.asc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    return templates.TemplateResponse(
        "damages/page.html",
        {
            "request": request,
            "damages": damages,
            "locations": locations,
            "items": items_data,
            "active_page": "damages",
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )

@app.get("/damages/table", response_class=HTMLResponse)
async def damages_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    damages = db.query(DamageNote).order_by(DamageNote.id.desc()).all()
    return templates.TemplateResponse(
        "damages/table.html", {"request": request, "damages": damages}
    )

@app.post("/damages", response_class=HTMLResponse)
async def create_damage(
    request: Request,
    date: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    lot_code: List[str] = Form([]),
    qty: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if not location_id:
        damages = db.query(DamageNote).order_by(DamageNote.id.desc()).all()
        return templates.TemplateResponse(
            "damages/table.html",
            {"request": request, "damages": damages, "message": "يرجى اختيار المخزن."},
        )

    prepared = []
    error = None
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        code = clean_text(lot_code[idx] if idx < len(lot_code) else "")
        if not code:
            error = "يرجى إدخال رقم التشغيلة."
            break
        qty_val = parse_decimal(qty[idx] if idx < len(qty) else "0")
        if qty_val <= 0:
            error = "يرجى إدخال كمية صحيحة."
            break
        lot = (
            db.query(ItemLot)
            .filter(ItemLot.item_id == int(raw_item_id), ItemLot.lot_code == code)
            .first()
        )
        if not lot:
            error = f"لا توجد تشغيلة بهذا الكود {code}."
            break
        balance = get_lot_balance(db, int(location_id), lot.id)
        if qty_val > balance:
            error = f"الكمية المطلوبة أكبر من الرصيد المتاح للتشغيلة {code}."
            break
        prepared.append((int(raw_item_id), lot, qty_val))

    if error or not prepared:
        damages = db.query(DamageNote).order_by(DamageNote.id.desc()).all()
        return templates.TemplateResponse(
            "damages/table.html",
            {"request": request, "damages": damages, "message": error or "يرجى إدخال بيانات الأصناف."},
        )

    damage = DamageNote(
        date=parse_date(date),
        location_id=int(location_id),
        notes=clean_text(notes),
    )
    db.add(damage)
    db.flush()

    for item_id_val, lot, qty_val in prepared:
        db.add(
            DamageLine(
                damage_id=damage.id,
                item_id=item_id_val,
                lot_id=lot.id,
                lot_code_snapshot=lot.lot_code,
                qty=qty_val,
            )
        )
        db.add(
            InventoryMove(
                date=damage.date,
                location_id=int(location_id),
                item_id=item_id_val,
                lot_id=lot.id,
                qty_in=Decimal("0"),
                qty_out=qty_val,
                source_type="damage",
                source_id=damage.id,
                notes="هالك/تالف",
            )
        )

    db.commit()
    damages = db.query(DamageNote).order_by(DamageNote.id.desc()).all()
    return templates.TemplateResponse(
        "damages/table.html", {"request": request, "damages": damages}
    )

@app.delete("/damages/{damage_id}", response_class=HTMLResponse)
async def delete_damage(request: Request, damage_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    damage = db.query(DamageNote).filter(DamageNote.id == damage_id).first()
    if damage:
        db.query(InventoryMove).filter(
            InventoryMove.source_type == "damage",
            InventoryMove.source_id == damage.id,
        ).delete()
        db.delete(damage)
        db.commit()
    damages = db.query(DamageNote).order_by(DamageNote.id.desc()).all()
    return templates.TemplateResponse(
        "damages/table.html", {"request": request, "damages": damages}
    )

# -------------------------
# Locations + Pharmacies view
# -------------------------
@app.get("/locations", response_class=HTMLResponse)
async def read_locations(
    request: Request,
    type: Optional[str] = None,
    print_receipt_tx: Optional[int] = None,
    print_statement: Optional[str] = None,
    entity_id: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if print_receipt_tx:
        tx = db.query(LocationTransaction).filter(LocationTransaction.id == print_receipt_tx).first()
        if not tx or tx.type != "payment":
            return RedirectResponse(url="/locations", status_code=303)
        location = db.query(Location).filter(Location.id == tx.location_id).first()
        settings = get_print_settings()
        return templates.TemplateResponse(
            "locations/receipt_print.html",
            {"request": request, "tx": tx, "location": location, "settings": settings},
        )
    if print_statement and entity_id:
        transactions, totals = _entity_ledger_query(db, int(entity_id), start_date, end_date)
        opening_balance = _get_opening_balance(db, int(entity_id))
        location = db.query(Location).filter(Location.id == int(entity_id)).first()
        settings = get_print_settings()
        return templates.TemplateResponse(
            "locations/statement_print.html",
            {
                "request": request,
                "location": location,
                "transactions": transactions,
                "totals": totals,
                "opening_balance": opening_balance,
                "start_date": start_date or "",
                "end_date": end_date or "",
                "settings": settings,
            },
        )
    query = db.query(Location)
    if type:
        query = query.filter(Location.type == type)
    locations = query.order_by(Location.id.desc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "locations/page.html",
        {
            "request": request,
            "locations": locations,
            "reps": reps,
            "active_page": "locations",
            "show_type_select": True,
            "page_title": "الجهات",
            "transactions": [],
            "totals": {"balance": Decimal("0"), "total_debt": Decimal("0"), "total_paid": Decimal("0")},
            "opening_balance": Decimal("0"),
            "selected_entity_id": None,
            "start_date": "",
            "end_date": "",
        },
    )

@app.get("/locations/table", response_class=HTMLResponse)
async def read_locations_table(request: Request, type: Optional[str] = None, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    query = db.query(Location)
    if type:
        query = query.filter(Location.type == type)
    locations = query.order_by(Location.id.desc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "locations/table.html", {"request": request, "locations": locations, "reps": reps}
    )

@app.post("/locations", response_class=HTMLResponse)
async def create_location(
    request: Request,
    name: str = Form(...),
    type: str = Form("pharmacy"),
    governorate: str = Form(""),
    city: str = Form(""),
    address: str = Form(""),
    phone: str = Form(""),
    region: str = Form(""),
    notes: str = Form(""),
    rep_ids: List[str] = Form([]),
    rep_id: str = Form(""),
    opening_balance: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    clean_name = clean_text(name)
    if clean_name:
        rep_ids_clean = [int(r) for r in rep_ids if str(r).isdigit()]
        if not rep_ids_clean and str(rep_id).isdigit():
            rep_ids_clean = [int(rep_id)]
        first_rep_id = rep_ids_clean[0] if rep_ids_clean else None
        type_val = clean_text(type)
        if type_val not in {"pharmacy", "sub_warehouse", "warehouse"}:
            type_val = "sub_warehouse"
        governorate_val = clean_text(governorate) or clean_text(region)
        city_val = clean_text(city) or clean_text(address)
        location = Location(
            name=clean_name,
            type=type_val,
            address=city_val,
            phone=clean_text(phone),
            region=governorate_val,
            notes=clean_text(notes),
            rep_id=first_rep_id,
        )
        db.add(location)
        db.flush()
        if rep_ids_clean:
            reps = db.query(Representative).filter(Representative.id.in_(rep_ids_clean)).all()
            location.reps = reps
        opening_val = parse_decimal(opening_balance)
        if opening_val != 0:
            db.add(
                LocationTransaction(
                    date=datetime.now(),
                    location_id=location.id,
                    type="opening_balance",
                    amount=opening_val,
                    notes="رصيد أول المدة",
                )
            )
        db.commit()
    locations = db.query(Location).order_by(Location.id.desc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "locations/table.html", {"request": request, "locations": locations, "reps": reps}
    )

@app.delete("/locations/{location_id}", response_class=HTMLResponse)
async def delete_location(request: Request, location_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    location = db.query(Location).filter(Location.id == location_id).first()
    if location:
        db.delete(location)
        db.commit()
    locations = db.query(Location).order_by(Location.id.desc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "locations/table.html", {"request": request, "locations": locations, "reps": reps}
    )

@app.post("/locations/{location_id}/update", response_class=HTMLResponse)
async def update_location(
    request: Request,
    location_id: int,
    name: str = Form(...),
    type: str = Form("pharmacy"),
    governorate: str = Form(""),
    city: str = Form(""),
    address: str = Form(""),
    phone: str = Form(""),
    region: str = Form(""),
    notes: str = Form(""),
    rep_ids: List[str] = Form([]),
    rep_id: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    location = db.query(Location).filter(Location.id == location_id).first()
    clean_name = clean_text(name)
    if location and clean_name:
        rep_ids_clean = [int(r) for r in rep_ids if str(r).isdigit()]
        if not rep_ids_clean and str(rep_id).isdigit():
            rep_ids_clean = [int(rep_id)]
        first_rep_id = rep_ids_clean[0] if rep_ids_clean else None
        type_val = clean_text(type)
        if type_val not in {"pharmacy", "sub_warehouse", "warehouse"}:
            type_val = "sub_warehouse"
        governorate_val = clean_text(governorate) or clean_text(region)
        city_val = clean_text(city) or clean_text(address)
        location.name = clean_name
        location.type = type_val
        location.address = city_val
        location.phone = clean_text(phone)
        location.region = governorate_val
        location.notes = clean_text(notes)
        location.rep_id = first_rep_id
        if rep_ids_clean:
            reps = db.query(Representative).filter(Representative.id.in_(rep_ids_clean)).all()
            location.reps = reps
        else:
            location.reps = []
        db.commit()
    locations = db.query(Location).order_by(Location.id.desc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "locations/table.html", {"request": request, "locations": locations, "reps": reps}
    )

def _entity_ledger_query(
    db: Session, location_id: int, start_date: Optional[str], end_date: Optional[str]
):
    query = db.query(LocationTransaction).filter(LocationTransaction.location_id == location_id)
    if start_date:
        start_dt = datetime.combine(parse_date_only(start_date), datetime.min.time())
        query = query.filter(LocationTransaction.date >= start_dt)
    if end_date:
        end_dt = datetime.combine(parse_date_only(end_date), datetime.max.time())
        query = query.filter(LocationTransaction.date <= end_dt)
    transactions = query.order_by(LocationTransaction.date.desc(), LocationTransaction.id.desc()).all()
    balance = Decimal("0")
    total_debt = Decimal("0")
    total_paid = Decimal("0")
    for tx in transactions:
        amount = Decimal(str(tx.amount or 0))
        balance += amount
        if amount > 0:
            total_debt += amount
        elif amount < 0:
            total_paid += abs(amount)
    return transactions, {
        "balance": balance,
        "total_debt": total_debt,
        "total_paid": total_paid,
    }

def _get_opening_balance(db: Session, location_id: int) -> Decimal:
    value = (
        db.query(func.coalesce(func.sum(LocationTransaction.amount), 0))
        .filter(
            LocationTransaction.location_id == location_id,
            LocationTransaction.type == "opening_balance",
        )
        .scalar()
        or 0
    )
    return Decimal(str(value))

@app.get("/locations/accounts/table", response_class=HTMLResponse)
async def location_accounts_table(
    request: Request,
    location_id: str = "",
    start_date: str = "",
    end_date: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if not location_id:
        return templates.TemplateResponse(
            "locations/accounts_table.html",
            {
                "request": request,
                "transactions": [],
                "totals": {"balance": Decimal("0"), "total_debt": Decimal("0"), "total_paid": Decimal("0")},
                "message": "يرجى اختيار جهة.",
                "opening_balance": Decimal("0"),
                "selected_entity_id": None,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
    transactions, totals = _entity_ledger_query(db, int(location_id), start_date or None, end_date or None)
    opening_balance = _get_opening_balance(db, int(location_id))
    return templates.TemplateResponse(
        "locations/accounts_table.html",
        {
            "request": request,
            "transactions": transactions,
            "totals": totals,
            "opening_balance": opening_balance,
            "selected_entity_id": int(location_id),
            "start_date": start_date,
            "end_date": end_date,
        },
    )

@app.post("/locations/accounts/payment", response_class=HTMLResponse)
async def location_payment(
    request: Request,
    location_id: str = Form(...),
    date: str = Form(""),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amount_val = parse_decimal(amount)
    last_receipt_id = None
    if location_id and amount_val > 0:
        prev_balance = (
            db.query(func.coalesce(func.sum(LocationTransaction.amount), 0))
            .filter(LocationTransaction.location_id == int(location_id))
            .scalar()
            or 0
        )
        prev_balance = Decimal(str(prev_balance))
        new_balance = prev_balance - amount_val
        tx = LocationTransaction(
            date=parse_date(date),
            location_id=int(location_id),
            type="payment",
            amount=Decimal("0") - amount_val,
            notes=clean_text(notes) or "سداد",
            prev_balance_snapshot=prev_balance,
            new_balance_snapshot=new_balance,
        )
        db.add(tx)
        db.commit()
        last_receipt_id = tx.id
    transactions, totals = _entity_ledger_query(db, int(location_id), None, None)
    opening_balance = _get_opening_balance(db, int(location_id))
    return templates.TemplateResponse(
        "locations/accounts_table.html",
        {
            "request": request,
            "transactions": transactions,
            "totals": totals,
            "opening_balance": opening_balance,
            "last_receipt_id": last_receipt_id,
            "selected_entity_id": int(location_id),
            "start_date": "",
            "end_date": "",
        },
    )

@app.post("/locations/accounts/adjustment", response_class=HTMLResponse)
async def location_adjustment(
    request: Request,
    location_id: str = Form(...),
    date: str = Form(""),
    amount: str = Form("0"),
    direction: str = Form("+"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amount_val = parse_decimal(amount)
    if location_id and amount_val > 0:
        signed = amount_val if direction != "-" else Decimal("0") - amount_val
        db.add(
            LocationTransaction(
                date=parse_date(date),
                location_id=int(location_id),
                type="adjustment",
                amount=signed,
                notes=clean_text(notes),
            )
        )
        db.commit()
    transactions, totals = _entity_ledger_query(db, int(location_id), None, None)
    opening_balance = _get_opening_balance(db, int(location_id))
    return templates.TemplateResponse(
        "locations/accounts_table.html",
        {
            "request": request,
            "transactions": transactions,
            "totals": totals,
            "opening_balance": opening_balance,
            "selected_entity_id": int(location_id),
            "start_date": "",
            "end_date": "",
        },
    )

@app.post("/locations/accounts/opening", response_class=HTMLResponse)
async def location_opening_balance(
    request: Request,
    location_id: str = Form(...),
    opening_balance: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if location_id:
        db.query(LocationTransaction).filter(
            LocationTransaction.location_id == int(location_id),
            LocationTransaction.type == "opening_balance",
        ).delete()
        opening_val = parse_decimal(opening_balance)
        if opening_val != 0:
            db.add(
                LocationTransaction(
                    date=datetime.now(),
                    location_id=int(location_id),
                    type="opening_balance",
                    amount=opening_val,
                    notes="رصيد أول المدة",
                )
            )
        db.commit()
    transactions, totals = _entity_ledger_query(db, int(location_id), None, None)
    opening_balance = _get_opening_balance(db, int(location_id))
    return templates.TemplateResponse(
        "locations/accounts_table.html",
        {
            "request": request,
            "transactions": transactions,
            "totals": totals,
            "opening_balance": opening_balance,
            "selected_entity_id": int(location_id),
            "start_date": "",
            "end_date": "",
        },
    )

@app.delete("/locations/accounts/tx/{tx_id}", response_class=HTMLResponse)
async def location_delete_tx(request: Request, tx_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    tx = db.query(LocationTransaction).filter(LocationTransaction.id == tx_id).first()
    location_id = tx.location_id if tx else None
    if tx:
        db.delete(tx)
        db.commit()
    if not location_id:
        return templates.TemplateResponse(
            "locations/accounts_table.html",
            {
                "request": request,
                "transactions": [],
                "totals": {"balance": Decimal("0"), "total_debt": Decimal("0"), "total_paid": Decimal("0")},
                "opening_balance": Decimal("0"),
                "selected_entity_id": None,
                "start_date": "",
                "end_date": "",
            },
        )
    transactions, totals = _entity_ledger_query(db, int(location_id), None, None)
    opening_balance = _get_opening_balance(db, int(location_id))
    return templates.TemplateResponse(
        "locations/accounts_table.html",
        {
            "request": request,
            "transactions": transactions,
            "totals": totals,
            "opening_balance": opening_balance,
            "selected_entity_id": int(location_id),
            "start_date": "",
            "end_date": "",
        },
    )

# -------------------------
# Representatives
# -------------------------
@app.get("/reps", response_class=HTMLResponse)
async def read_reps(request: Request, db: Session = Depends(get_db), user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    reps = db.query(Representative).order_by(Representative.id.desc()).all()
    return templates.TemplateResponse(
        "reps/page.html",
        {
            "request": request,
            "reps": reps,
            "active_page": "reps",
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )

@app.get("/reps/section/{section}", response_class=HTMLResponse)
async def reps_section(request: Request, section: str, db: Session = Depends(get_db),user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    today_dt = datetime.now()
    today_month = today_dt.month
    today_year = today_dt.year
    if section == "adjustments":
        additions = db.query(EmployeeAddition).order_by(EmployeeAddition.id.desc()).all()
        deductions = db.query(EmployeeDeduction).order_by(EmployeeDeduction.id.desc()).all()
        adjustments = []
        for row in additions:
            adjustments.append(
                {
                    "id": row.id,
                    "date": row.date,
                    "rep": row.rep,
                    "amount": row.amount,
                    "reason": row.reason,
                    "notes": row.notes,
                    "kind": "addition",
                    "created_at": row.created_at,
                }
            )
        for row in deductions:
            adjustments.append(
                {
                    "id": row.id,
                    "date": row.date,
                    "rep": row.rep,
                    "amount": row.amount,
                    "reason": row.reason,
                    "notes": row.notes,
                    "kind": "deduction",
                    "created_at": row.created_at,
                }
            )
        adjustments.sort(key=lambda r: r.get("created_at") or datetime.min, reverse=True)
        return templates.TemplateResponse(
            "reps/sections/adjustments.html",
            {
                "request": request,
                "reps": reps,
                "adjustments": adjustments,
                "today": today_dt.strftime("%Y-%m-%d"),
                "today_month": today_month,
                "today_year": today_year,
            },
        )
    if section == "additions":
        additions = db.query(EmployeeAddition).order_by(EmployeeAddition.id.desc()).all()
        return templates.TemplateResponse(
            "reps/sections/additions.html",
            {
                "request": request,
                "reps": reps,
                "additions": additions,
                "today": today_dt.strftime("%Y-%m-%d"),
                "today_month": today_month,
                "today_year": today_year,
            },
        )
    if section == "deductions":
        deductions = db.query(EmployeeDeduction).order_by(EmployeeDeduction.id.desc()).all()
        return templates.TemplateResponse(
            "reps/sections/deductions.html",
            {
                "request": request,
                "reps": reps,
                "deductions": deductions,
                "today": today_dt.strftime("%Y-%m-%d"),
                "today_month": today_month,
                "today_year": today_year,
            },
        )
    if section == "salaries":
        month_param = int(request.query_params.get("month") or today_month)
        year_param = int(request.query_params.get("year") or today_year)
        additions = (
            db.query(EmployeeAddition.rep_id, func.coalesce(func.sum(EmployeeAddition.amount), 0))
            .filter(EmployeeAddition.month == month_param, EmployeeAddition.year == year_param)
            .group_by(EmployeeAddition.rep_id)
            .all()
        )
        deductions = (
            db.query(EmployeeDeduction.rep_id, func.coalesce(func.sum(EmployeeDeduction.amount), 0))
            .filter(EmployeeDeduction.month == month_param, EmployeeDeduction.year == year_param)
            .group_by(EmployeeDeduction.rep_id)
            .all()
        )
        paid_rows = (
            db.query(EmployeeSalary.rep_id, func.coalesce(func.sum(EmployeeSalary.amount), 0))
            .filter(EmployeeSalary.month == month_param, EmployeeSalary.year == year_param)
            .group_by(EmployeeSalary.rep_id)
            .all()
        )
        add_map = {row[0]: Decimal(str(row[1] or 0)) for row in additions}
        ded_map = {row[0]: Decimal(str(row[1] or 0)) for row in deductions}
        paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in paid_rows}
        salary_rows = []
        for rep in reps:
            base_salary = Decimal(str(rep.base_salary or 0))
            add_val = add_map.get(rep.id, Decimal("0"))
            ded_val = ded_map.get(rep.id, Decimal("0"))
            net_val = base_salary + add_val - ded_val
            salary_rows.append(
                {
                    "rep": rep,
                    "base_salary": base_salary,
                    "additions": add_val,
                    "deductions": ded_val,
                    "net_salary": net_val,
                    "paid_amount": paid_map.get(rep.id, Decimal("0")),
                }
            )
        return templates.TemplateResponse(
            "reps/sections/salaries.html",
            {
                "request": request,
                "reps": reps,
                "salaries": salary_rows,
                "today": today_dt.strftime("%Y-%m-%d"),
                "selected_month": month_param,
                "selected_year": year_param,
            },
        )
    if section == "reports":
        return templates.TemplateResponse(
            "reps/sections/reports.html",
            {
                "request": request,
            },
        )
    reps_list = db.query(Representative).order_by(Representative.id.desc()).all()
    return templates.TemplateResponse(
        "reps/sections/employees.html",
        {"request": request, "reps": reps_list, "today": today_dt.strftime("%Y-%m-%d")},
    )

@app.get("/reps/table", response_class=HTMLResponse)
async def read_reps_table(request: Request, db: Session = Depends(get_db),user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    reps = db.query(Representative).order_by(Representative.id.desc()).all()
    return templates.TemplateResponse("reps/table.html", {"request": request, "reps": reps})

@app.post("/reps", response_class=HTMLResponse)
async def create_rep(
    request: Request,
    name: str = Form(...),
    code: str = Form(""),
    phone: str = Form(""),
    home_phone: str = Form(""),
    mobile: str = Form(""),
    address: str = Form(""),
    governorate: str = Form(""),
    city: str = Form(""),
    region: str = Form(""),
    birth_date: str = Form(""),
    gender: str = Form(""),
    national_id: str = Form(""),
    job_title: str = Form(""),
    supervisor: str = Form(""),
    hire_date: str = Form(""),
    base_salary: str = Form(""),
    hourly_rate: str = Form(""),
    insurance_no: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    if any(
        has_broken_text(val)
        for val in [
            name,
            code,
            phone,
            home_phone,
            mobile,
            address,
            governorate,
            city,
            region,
            gender,
            national_id,
            job_title,
            supervisor,
            insurance_no,
            notes,
        ]
    ):
        return HTMLResponse(
            "البيانات تحتوي على رموز غير صالحة. يرجى إعادة الإدخال.",
            status_code=400,
        )
    clean_name = clean_text(name)
    if clean_name:
        db.add(
            Representative(
                name=clean_name,
                code=clean_text(code),
                phone=clean_text(phone),
                home_phone=clean_text(home_phone),
                mobile=clean_text(mobile),
                address=clean_text(address),
                governorate=clean_text(governorate),
                city=clean_text(city),
                region=clean_text(region),
                birth_date=parse_date_only(birth_date) if birth_date else None,
                gender=clean_text(gender),
                national_id=clean_text(national_id),
                job_title=clean_text(job_title),
                supervisor=clean_text(supervisor),
                hire_date=parse_date_only(hire_date) if hire_date else None,
                base_salary=parse_decimal(base_salary),
                hourly_rate=parse_decimal(hourly_rate),
                insurance_no=clean_text(insurance_no),
                notes=clean_text(notes),
            )
        )
        db.commit()
        ensure_cash_accounts(db)
    reps = db.query(Representative).order_by(Representative.id.desc()).all()
    return templates.TemplateResponse("reps/table.html", {"request": request, "reps": reps})

@app.delete("/reps/{rep_id}", response_class=HTMLResponse)
async def delete_rep(request: Request, rep_id: int, db: Session = Depends(get_db),user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    rep = db.query(Representative).filter(Representative.id == rep_id).first()
    if rep:
        db.delete(rep)
        db.commit()
    reps = db.query(Representative).order_by(Representative.id.desc()).all()
    return templates.TemplateResponse("reps/table.html", {"request": request, "reps": reps})

@app.post("/reps/{rep_id}/update", response_class=HTMLResponse)
async def update_rep(
    request: Request,
    rep_id: int,
    name: str = Form(...),
    phone: str = Form(""),
    mobile: str = Form(""),
    job_title: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    if any(has_broken_text(val) for val in [name, phone, mobile, job_title, notes]):
        return HTMLResponse(
            "البيانات تحتوي على رموز غير صالحة. يرجى إعادة الإدخال.",
            status_code=400,
        )
    rep = db.query(Representative).filter(Representative.id == rep_id).first()
    clean_name = clean_text(name)
    if rep and clean_name:
        rep.name = clean_name
        rep.phone = clean_text(phone)
        rep.mobile = clean_text(mobile)
        rep.job_title = clean_text(job_title)
        rep.notes = clean_text(notes)
        db.commit()
    reps = db.query(Representative).order_by(Representative.id.desc()).all()
    return templates.TemplateResponse("reps/table.html", {"request": request, "reps": reps})

@app.post("/reps/additions", response_class=HTMLResponse)
async def create_rep_addition(
    request: Request,
    rep_id: str = Form(...),
    date: str = Form(""),
    month: str = Form(""),
    year: str = Form(""),
    reason: str = Form(""),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    clean_date = parse_date_only(date)
    month_val = int(month) if month else (clean_date.month if clean_date else datetime.now().month)
    year_val = int(year) if year else (clean_date.year if clean_date else datetime.now().year)
    if rep_id and amt > 0:
        db.add(
            EmployeeAddition(
                rep_id=int(rep_id),
                date=clean_date,
                month=month_val,
                year=year_val,
                reason=clean_text(reason),
                amount=amt,
                notes=clean_text(notes),
            )
        )
        db.commit()
    additions = db.query(EmployeeAddition).order_by(EmployeeAddition.id.desc()).all()
    return templates.TemplateResponse(
        "reps/additions_table.html", {"request": request, "additions": additions}
    )

@app.post("/reps/deductions", response_class=HTMLResponse)
async def create_rep_deduction(
    request: Request,
    rep_id: str = Form(...),
    date: str = Form(""),
    month: str = Form(""),
    year: str = Form(""),
    reason: str = Form(""),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    clean_date = parse_date_only(date)
    month_val = int(month) if month else (clean_date.month if clean_date else datetime.now().month)
    year_val = int(year) if year else (clean_date.year if clean_date else datetime.now().year)
    if rep_id and amt > 0:
        db.add(
            EmployeeDeduction(
                rep_id=int(rep_id),
                date=clean_date,
                month=month_val,
                year=year_val,
                reason=clean_text(reason),
                amount=amt,
                notes=clean_text(notes),
            )
        )
        db.commit()
    deductions = db.query(EmployeeDeduction).order_by(EmployeeDeduction.id.desc()).all()
    return templates.TemplateResponse(
        "reps/deductions_table.html", {"request": request, "deductions": deductions}
    )

@app.post("/reps/adjustments", response_class=HTMLResponse)
async def create_rep_adjustment(
    request: Request,
    rep_id: str = Form(...),
    action: str = Form("addition"),
    date: str = Form(""),
    month: str = Form(""),
    year: str = Form(""),
    reason: str = Form(""),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    clean_date = parse_date_only(date)
    month_val = int(month) if month else (clean_date.month if clean_date else datetime.now().month)
    year_val = int(year) if year else (clean_date.year if clean_date else datetime.now().year)
    if rep_id and amt > 0:
        if action == "deduction":
            db.add(
                EmployeeDeduction(
                    rep_id=int(rep_id),
                    date=clean_date,
                    month=month_val,
                    year=year_val,
                    reason=clean_text(reason),
                    amount=amt,
                    notes=clean_text(notes),
                )
            )
        else:
            db.add(
                EmployeeAddition(
                    rep_id=int(rep_id),
                    date=clean_date,
                    month=month_val,
                    year=year_val,
                    reason=clean_text(reason),
                    amount=amt,
                    notes=clean_text(notes),
                )
            )
        db.commit()
    additions = db.query(EmployeeAddition).order_by(EmployeeAddition.id.desc()).all()
    deductions = db.query(EmployeeDeduction).order_by(EmployeeDeduction.id.desc()).all()
    adjustments = []
    for row in additions:
        adjustments.append(
            {
                "id": row.id,
                "date": row.date,
                "rep": row.rep,
                "amount": row.amount,
                "reason": row.reason,
                "notes": row.notes,
                "kind": "addition",
                "month": row.month,
                "year": row.year,
                "created_at": row.created_at,
            }
        )
    for row in deductions:
        adjustments.append(
            {
                "id": row.id,
                "date": row.date,
                "rep": row.rep,
                "amount": row.amount,
                "reason": row.reason,
                "notes": row.notes,
                "kind": "deduction",
                "month": row.month,
                "year": row.year,
                "created_at": row.created_at,
            }
        )
    adjustments.sort(key=lambda r: r.get("created_at") or datetime.min, reverse=True)
    return templates.TemplateResponse(
        "reps/adjustments_table.html", {"request": request, "adjustments": adjustments}
    )

@app.post("/reps/salaries", response_class=HTMLResponse)
async def create_rep_salary(
    request: Request,
    rep_id: str = Form(...),
    month: str = Form(""),
    year: str = Form(""),
    date: str = Form(""),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    month_val = int(month) if month else 0
    year_val = int(year) if year else 0
    if rep_id and amt > 0 and month_val > 0 and year_val > 0:
        db.add(
            EmployeeSalary(
                rep_id=int(rep_id),
                month=month_val,
                year=year_val,
                date=parse_date_only(date),
                amount=amt,
                notes=clean_text(notes),
            )
        )
        db.commit()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    additions = (
        db.query(EmployeeAddition.rep_id, func.coalesce(func.sum(EmployeeAddition.amount), 0))
        .filter(EmployeeAddition.month == month_val, EmployeeAddition.year == year_val)
        .group_by(EmployeeAddition.rep_id)
        .all()
    )
    deductions = (
        db.query(EmployeeDeduction.rep_id, func.coalesce(func.sum(EmployeeDeduction.amount), 0))
        .filter(EmployeeDeduction.month == month_val, EmployeeDeduction.year == year_val)
        .group_by(EmployeeDeduction.rep_id)
        .all()
    )
    paid_rows = (
        db.query(EmployeeSalary.rep_id, func.coalesce(func.sum(EmployeeSalary.amount), 0))
        .filter(EmployeeSalary.month == month_val, EmployeeSalary.year == year_val)
        .group_by(EmployeeSalary.rep_id)
        .all()
    )
    add_map = {row[0]: Decimal(str(row[1] or 0)) for row in additions}
    ded_map = {row[0]: Decimal(str(row[1] or 0)) for row in deductions}
    paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in paid_rows}
    salary_rows = []
    for rep in reps:
        base_salary = Decimal(str(rep.base_salary or 0))
        add_val = add_map.get(rep.id, Decimal("0"))
        ded_val = ded_map.get(rep.id, Decimal("0"))
        net_val = base_salary + add_val - ded_val
        salary_rows.append(
            {
                "rep": rep,
                "base_salary": base_salary,
                "additions": add_val,
                "deductions": ded_val,
                "net_salary": net_val,
                "paid_amount": paid_map.get(rep.id, Decimal("0")),
            }
        )
    return templates.TemplateResponse(
        "reps/salaries_table.html", {"request": request, "salaries": salary_rows}
    )

@app.post("/reps/salaries/pay", response_class=HTMLResponse)
async def pay_rep_salaries(
    request: Request,
    month: str = Form(...),
    year: str = Form(...),
    db: Session = Depends(get_db),
    user = Depends(backend.app.auth.require_perm("reps.manage"))):
    if isinstance(user, RedirectResponse):
        return user
    month_val = int(month) if month else datetime.now().month
    year_val = int(year) if year else datetime.now().year
    reps = db.query(Representative).order_by(Representative.name.asc()).all()

    additions = (
        db.query(EmployeeAddition.rep_id, func.coalesce(func.sum(EmployeeAddition.amount), 0))
        .filter(EmployeeAddition.month == month_val, EmployeeAddition.year == year_val)
        .group_by(EmployeeAddition.rep_id)
        .all()
    )
    deductions = (
        db.query(EmployeeDeduction.rep_id, func.coalesce(func.sum(EmployeeDeduction.amount), 0))
        .filter(EmployeeDeduction.month == month_val, EmployeeDeduction.year == year_val)
        .group_by(EmployeeDeduction.rep_id)
        .all()
    )
    add_map = {row[0]: Decimal(str(row[1] or 0)) for row in additions}
    ded_map = {row[0]: Decimal(str(row[1] or 0)) for row in deductions}

    total_paid = Decimal("0")
    for rep in reps:
        existing = (
            db.query(EmployeeSalary)
            .filter(EmployeeSalary.rep_id == rep.id, EmployeeSalary.month == month_val, EmployeeSalary.year == year_val)
            .first()
        )
        if existing:
            continue
        base_salary = Decimal(str(rep.base_salary or 0))
        net_val = base_salary + add_map.get(rep.id, Decimal("0")) - ded_map.get(rep.id, Decimal("0"))
        if net_val <= 0:
            continue
        db.add(
            EmployeeSalary(
                rep_id=rep.id,
                month=month_val,
                year=year_val,
                date=datetime.now().date(),
                amount=net_val,
                notes=f"رواتب شهر {month_val}/{year_val}",
            )
        )
        total_paid += net_val
    db.commit()

    if total_paid > 0:
        main_cash = get_main_cash_account(db)
        db.add(
            CashTransaction(
                date=datetime.now(),
                type="salary_payment",
                amount=total_paid,
                from_account_id=main_cash.id,
                to_account_id=None,
                notes=f"صرف رواتب شهر {month_val}/{year_val}",
                source_type="salary",
                source_id=None,
            )
        )
        db.commit()

    paid_rows = (
        db.query(EmployeeSalary.rep_id, func.coalesce(func.sum(EmployeeSalary.amount), 0))
        .filter(EmployeeSalary.month == month_val, EmployeeSalary.year == year_val)
        .group_by(EmployeeSalary.rep_id)
        .all()
    )
    paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in paid_rows}
    salary_rows = []
    for rep in reps:
        base_salary = Decimal(str(rep.base_salary or 0))
        add_val = add_map.get(rep.id, Decimal("0"))
        ded_val = ded_map.get(rep.id, Decimal("0"))
        net_val = base_salary + add_val - ded_val
        salary_rows.append(
            {
                "rep": rep,
                "base_salary": base_salary,
                "additions": add_val,
                "deductions": ded_val,
                "net_salary": net_val,
                "paid_amount": paid_map.get(rep.id, Decimal("0")),
            }
        )

    return templates.TemplateResponse(
        "reps/salaries_table.html",
        {"request": request, "salaries": salary_rows},
    )

# -------------------------
# Doctors
# -------------------------
@app.get("/doctors", response_class=HTMLResponse)
async def read_doctors(request: Request, q: Optional[str] = None, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
    if q:
        like = f"%{q.strip()}%"
        doctors = db.query(Doctor).filter(Doctor.name.ilike(like)).order_by(Doctor.id.desc()).all()
    return templates.TemplateResponse(
        "doctors/page.html",
        {"request": request, "doctors": doctors, "active_page": "doctors", "q": q or ""},
    )


@app.get("/doctors/table", response_class=HTMLResponse)
async def read_doctors_table(request: Request, q: Optional[str] = None, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
    if q:
        like = f"%{q.strip()}%"
        doctors = db.query(Doctor).filter(Doctor.name.ilike(like)).order_by(Doctor.id.desc()).all()
    return templates.TemplateResponse("doctors/table.html", {"request": request, "doctors": doctors})


@app.post("/doctors", response_class=HTMLResponse)
async def create_doctor(
    request: Request,
    name: str = Form(...),
    phone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    clean_name = clean_text(name)
    if clean_name:
        doctor = Doctor(name=clean_name, phone=clean_text(phone), notes=clean_text(notes))
        db.add(doctor)
        db.commit()
    doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
    return templates.TemplateResponse("doctors/table.html", {"request": request, "doctors": doctors})


@app.post("/doctors/{doctor_id}/update", response_class=HTMLResponse)
async def update_doctor(
    request: Request,
    doctor_id: int,
    name: str = Form(...),
    phone: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    clean_name = clean_text(name)
    if doctor and clean_name:
        doctor.name = clean_name
        doctor.phone = clean_text(phone)
        doctor.notes = clean_text(notes)
        db.commit()
    doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
    return templates.TemplateResponse("doctors/table.html", {"request": request, "doctors": doctors})


@app.delete("/doctors/{doctor_id}", response_class=HTMLResponse)
async def delete_doctor(request: Request, doctor_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    doctor = db.query(Doctor).filter(Doctor.id == doctor_id).first()
    if doctor:
        db.delete(doctor)
        db.commit()
    doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
    return templates.TemplateResponse("doctors/table.html", {"request": request, "doctors": doctors})


@app.post("/doctors/{doctor_id}/opening", response_class=HTMLResponse)
async def set_doctor_opening_balance(
    request: Request,
    doctor_id: int,
    amount: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    db.query(DoctorTransaction).filter(
        DoctorTransaction.doctor_id == doctor_id,
        DoctorTransaction.type == "opening_balance",
    ).delete()
    if amt != 0:
        db.add(
            DoctorTransaction(
                date=datetime.now(),
                doctor_id=doctor_id,
                type="opening_balance",
                amount=amt,
                notes="رصيد أول المدة",
            )
        )
    db.commit()
    doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
    opening_rows = (
        db.query(DoctorTransaction.doctor_id, func.coalesce(func.sum(DoctorTransaction.amount), 0))
        .filter(DoctorTransaction.type == "opening_balance")
        .group_by(DoctorTransaction.doctor_id)
        .all()
    )
    opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
    return templates.TemplateResponse(
        "doctors/table.html", {"request": request, "doctors": doctors}
    )


@app.post("/doctors/opening", response_class=HTMLResponse)
async def set_doctor_opening_balance_page(
    request: Request,
    doctor_id: str = Form(...),
    amount: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    clean_id = int(doctor_id)
    db.query(DoctorTransaction).filter(
        DoctorTransaction.doctor_id == clean_id,
        DoctorTransaction.type == "opening_balance",
    ).delete()
    if amt != 0:
        db.add(
            DoctorTransaction(
                date=datetime.now(),
                doctor_id=clean_id,
                type="opening_balance",
                amount=amt,
                notes="رصيد أول المدة",
            )
        )
    db.commit()
    doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
    opening_rows = (
        db.query(DoctorTransaction.doctor_id, func.coalesce(func.sum(DoctorTransaction.amount), 0))
        .filter(DoctorTransaction.type == "opening_balance")
        .group_by(DoctorTransaction.doctor_id)
        .all()
    )
    opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
    return templates.TemplateResponse(
        "doctors/opening_table.html",
        {"request": request, "doctors": doctors, "opening_map": opening_map},
    )


@app.get("/doctors/section/{section}", response_class=HTMLResponse)
async def doctors_section(request: Request, section: str, q: Optional[str] = None, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if section == "opening":
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        opening_rows = (
            db.query(DoctorTransaction.doctor_id, func.coalesce(func.sum(DoctorTransaction.amount), 0))
            .filter(DoctorTransaction.type == "opening_balance")
            .group_by(DoctorTransaction.doctor_id)
            .all()
        )
        opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
        return templates.TemplateResponse(
            "doctors/sections/opening.html",
            {"request": request, "doctors": doctors, "opening_map": opening_map},
        )
    if section == "list":
        doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
        if q:
            like = f"%{q.strip()}%"
            doctors = db.query(Doctor).filter(Doctor.name.ilike(like)).order_by(Doctor.id.desc()).all()
        return templates.TemplateResponse(
            "doctors/sections/list.html",
            {"request": request, "doctors": doctors, "q": q or ""},
        )
    if section == "reports":
        return templates.TemplateResponse(
            "doctors/sections/reports.html",
            {"request": request},
        )
    return RedirectResponse(url="/doctors", status_code=303)


# -------------------------
# Doctor Commission
# -------------------------
@app.get("/commission/rules", response_class=HTMLResponse)
async def commission_rules(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    is_hx = request.headers.get("HX-Request") == "true"
    rules = db.query(DoctorCommissionRule).order_by(DoctorCommissionRule.id.desc()).all()
    doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
    pharmacies = (
        db.query(Location)
        .filter(Location.type == "pharmacy")
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    return templates.TemplateResponse(
        "commission/rules_section.html" if is_hx else "commission/rules_page.html",
        {
            "request": request,
            "rules": rules,
            "doctors": doctors,
            "pharmacies": pharmacies,
            "items": items,
            "active_page": "commission",
        },
    )


@app.get("/commission/rules/table", response_class=HTMLResponse)
async def commission_rules_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rules = db.query(DoctorCommissionRule).order_by(DoctorCommissionRule.id.desc()).all()
    return templates.TemplateResponse(
        "commission/rules_table.html", {"request": request, "rules": rules}
    )


@app.post("/commission/rules", response_class=HTMLResponse)
async def commission_rules_create(
    request: Request,
    doctor_id: str = Form(...),
    pharmacy_id: str = Form(...),
    item_id: str = Form(...),
    commission_type: str = Form("percent"),
    commission_value: str = Form("0"),
    active: Optional[str] = Form(None),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rule = (
        db.query(DoctorCommissionRule)
        .filter(
            DoctorCommissionRule.doctor_id == int(doctor_id),
            DoctorCommissionRule.pharmacy_location_id == int(pharmacy_id),
            DoctorCommissionRule.item_id == int(item_id),
        )
        .first()
    )
    if not rule:
        rule = DoctorCommissionRule(
            doctor_id=int(doctor_id),
            pharmacy_location_id=int(pharmacy_id),
            item_id=int(item_id),
            commission_type=commission_type,
            commission_value=parse_decimal(commission_value),
            active=bool(active),
            notes=clean_text(notes),
        )
        db.add(rule)
    else:
        rule.commission_type = commission_type
        rule.commission_value = parse_decimal(commission_value)
        rule.active = bool(active)
        rule.notes = clean_text(notes)
    db.commit()
    rules = db.query(DoctorCommissionRule).order_by(DoctorCommissionRule.id.desc()).all()
    return templates.TemplateResponse(
        "commission/rules_table.html", {"request": request, "rules": rules}
    )


@app.post("/commission/rules/{rule_id}/update", response_class=HTMLResponse)
async def commission_rules_update(
    request: Request,
    rule_id: int,
    commission_type: str = Form("percent"),
    commission_value: str = Form("0"),
    active: Optional[str] = Form(None),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rule = db.query(DoctorCommissionRule).filter(DoctorCommissionRule.id == rule_id).first()
    if rule:
        rule.commission_type = commission_type
        rule.commission_value = parse_decimal(commission_value)
        rule.active = bool(active)
        rule.notes = clean_text(notes)
        db.commit()
    rules = db.query(DoctorCommissionRule).order_by(DoctorCommissionRule.id.desc()).all()
    return templates.TemplateResponse(
        "commission/rules_table.html", {"request": request, "rules": rules}
    )


@app.post("/commission/rules/{rule_id}/toggle", response_class=HTMLResponse)
async def commission_rules_toggle(request: Request, rule_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rule = db.query(DoctorCommissionRule).filter(DoctorCommissionRule.id == rule_id).first()
    if rule:
        rule.active = not bool(rule.active)
        db.commit()
    rules = db.query(DoctorCommissionRule).order_by(DoctorCommissionRule.id.desc()).all()
    return templates.TemplateResponse(
        "commission/rules_table.html", {"request": request, "rules": rules}
    )


@app.delete("/commission/rules/{rule_id}", response_class=HTMLResponse)
async def commission_rules_delete(request: Request, rule_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rule = db.query(DoctorCommissionRule).filter(DoctorCommissionRule.id == rule_id).first()
    if rule:
        db.delete(rule)
        db.commit()
    rules = db.query(DoctorCommissionRule).order_by(DoctorCommissionRule.id.desc()).all()
    return templates.TemplateResponse(
        "commission/rules_table.html", {"request": request, "rules": rules}
    )


def _doctor_ledger_totals(transactions: List[DoctorTransaction]):
    balance = sum((t.amount or 0) for t in transactions)
    return {"balance": balance}


@app.get("/commission/ledger", response_class=HTMLResponse)
async def commission_ledger(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    is_hx = request.headers.get("HX-Request") == "true"
    doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
    return templates.TemplateResponse(
        "commission/doctor_ledger_section.html" if is_hx else "commission/doctor_ledger_page.html",
        {"request": request, "doctors": doctors, "transactions": [], "totals": {"balance": 0}},
    )


@app.get("/commission/ledger/table", response_class=HTMLResponse)
async def commission_ledger_table(
    request: Request,
    doctor_id: str = "",
    start_date: str = "",
    end_date: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    if not doctor_id:
        return templates.TemplateResponse(
            "commission/doctor_ledger_table.html",
            {"request": request, "transactions": [], "totals": {"balance": 0}},
        )
    query = db.query(DoctorTransaction).filter(DoctorTransaction.doctor_id == int(doctor_id))
    if start_date:
        query = query.filter(DoctorTransaction.date >= parse_date(start_date))
    if end_date:
        query = query.filter(DoctorTransaction.date <= parse_date_end(end_date))
    transactions = query.order_by(DoctorTransaction.date.desc()).all()
    totals = _doctor_ledger_totals(transactions)
    return templates.TemplateResponse(
        "commission/doctor_ledger_table.html",
        {"request": request, "transactions": transactions, "totals": totals},
    )


@app.post("/commission/ledger/payment", response_class=HTMLResponse)
async def commission_ledger_payment(
    request: Request,
    doctor_id: str = Form(...),
    date: str = Form(""),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    if amt > 0:
        db.add(
            DoctorTransaction(
                date=parse_date(date),
                doctor_id=int(doctor_id),
                type="payment",
                amount=Decimal("0") - amt,
                notes=clean_text(notes),
            )
        )
        db.commit()
    transactions = (
        db.query(DoctorTransaction)
        .filter(DoctorTransaction.doctor_id == int(doctor_id))
        .order_by(DoctorTransaction.date.desc())
        .all()
    )
    totals = _doctor_ledger_totals(transactions)
    return templates.TemplateResponse(
        "commission/doctor_ledger_table.html",
        {"request": request, "transactions": transactions, "totals": totals},
    )


@app.post("/commission/ledger/adjustment", response_class=HTMLResponse)
async def commission_ledger_adjustment(
    request: Request,
    doctor_id: str = Form(...),
    date: str = Form(""),
    amount: str = Form("0"),
    direction: str = Form("+"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    if amt > 0:
        signed_amount = amt if direction == "+" else Decimal("0") - amt
        db.add(
            DoctorTransaction(
                date=parse_date(date),
                doctor_id=int(doctor_id),
                type="adjustment",
                amount=signed_amount,
                notes=clean_text(notes),
            )
        )
        db.commit()
    transactions = (
        db.query(DoctorTransaction)
        .filter(DoctorTransaction.doctor_id == int(doctor_id))
        .order_by(DoctorTransaction.date.desc())
        .all()
    )
    totals = _doctor_ledger_totals(transactions)
    return templates.TemplateResponse(
        "commission/doctor_ledger_table.html",
        {"request": request, "transactions": transactions, "totals": totals},
    )


@app.post("/commission/ledger/tx/{tx_id}/update", response_class=HTMLResponse)
async def commission_ledger_update(
    request: Request,
    tx_id: int,
    date: str = Form(""),
    amount: str = Form("0"),
    direction: str = Form("+"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    tx = db.query(DoctorTransaction).filter(DoctorTransaction.id == tx_id).first()
    if tx:
        amt = parse_decimal(amount)
        tx.date = parse_date(date)
        tx.amount = amt if direction == "+" else Decimal("0") - amt
        tx.notes = clean_text(notes)
        db.commit()
        doctor_id = tx.doctor_id
    else:
        doctor_id = None
    transactions = (
        db.query(DoctorTransaction)
        .filter(DoctorTransaction.doctor_id == doctor_id)
        .order_by(DoctorTransaction.date.desc())
        .all()
        if doctor_id
        else []
    )
    totals = _doctor_ledger_totals(transactions)
    return templates.TemplateResponse(
        "commission/doctor_ledger_table.html",
        {"request": request, "transactions": transactions, "totals": totals},
    )


@app.delete("/commission/ledger/tx/{tx_id}", response_class=HTMLResponse)
async def commission_ledger_delete(request: Request, tx_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    tx = db.query(DoctorTransaction).filter(DoctorTransaction.id == tx_id).first()
    doctor_id = tx.doctor_id if tx else None
    if tx:
        db.delete(tx)
        db.commit()
    transactions = (
        db.query(DoctorTransaction)
        .filter(DoctorTransaction.doctor_id == doctor_id)
        .order_by(DoctorTransaction.date.desc())
        .all()
        if doctor_id
        else []
    )
    totals = _doctor_ledger_totals(transactions)
    return templates.TemplateResponse(
        "commission/doctor_ledger_table.html",
        {"request": request, "transactions": transactions, "totals": totals},
    )


# -------------------------
# Suppliers
# -------------------------
@app.get("/suppliers", response_class=HTMLResponse)
async def read_suppliers(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    suppliers = db.query(Supplier).order_by(Supplier.id.desc()).all()
    return templates.TemplateResponse(
        "suppliers/page.html",
        {"request": request, "suppliers": suppliers, "active_page": "suppliers"},
    )


@app.get("/suppliers/section/{section}", response_class=HTMLResponse)
async def suppliers_section(request: Request, section: str, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    suppliers = db.query(Supplier).order_by(Supplier.id.desc()).all()
    if section == "suppliers":
        return templates.TemplateResponse(
            "suppliers/sections/suppliers_list.html",
            {"request": request, "suppliers": suppliers},
        )
    if section == "adjustments":
        adjustments = (
            db.query(SupplierAdjustment).order_by(SupplierAdjustment.id.desc()).all()
        )
        return templates.TemplateResponse(
            "suppliers/sections/adjustments.html",
            {
                "request": request,
                "suppliers": suppliers,
                "adjustments": adjustments,
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    if section == "reports":
        return templates.TemplateResponse(
            "suppliers/sections/reports.html",
            {
                "request": request,
            },
        )
    return templates.TemplateResponse(
        "suppliers/sections/suppliers_list.html",
        {"request": request, "suppliers": suppliers},
    )


@app.get("/suppliers/table", response_class=HTMLResponse)
async def read_suppliers_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    suppliers = db.query(Supplier).order_by(Supplier.id.desc()).all()
    return templates.TemplateResponse("suppliers/table.html", {"request": request, "suppliers": suppliers})


@app.post("/suppliers", response_class=HTMLResponse)
async def create_supplier(
    request: Request,
    name: str = Form(...),
    contact_name: str = Form(""),
    contact_phone: str = Form(""),
    phone: str = Form(""),
    phone2: str = Form(""),
    fax: str = Form(""),
    address: str = Form(""),
    governorate: str = Form(""),
    city: str = Form(""),
    region: str = Form(""),
    email: str = Form(""),
    website: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    clean_name = clean_text(name)
    if clean_name:
        db.add(
            Supplier(
                name=clean_name,
                contact_name=clean_text(contact_name),
                contact_phone=clean_text(contact_phone),
                phone=clean_text(phone),
                phone2=clean_text(phone2),
                fax=clean_text(fax),
                address=clean_text(address),
                governorate=clean_text(governorate),
                city=clean_text(city),
                region=clean_text(region),
                email=clean_text(email),
                website=clean_text(website),
                notes=clean_text(notes),
            )
        )
        db.commit()
    suppliers = db.query(Supplier).order_by(Supplier.id.desc()).all()
    return templates.TemplateResponse("suppliers/table.html", {"request": request, "suppliers": suppliers})


@app.post("/suppliers/{supplier_id}/update", response_class=HTMLResponse)
async def update_supplier(
    request: Request,
    supplier_id: int,
    name: str = Form(...),
    contact_name: str = Form(""),
    contact_phone: str = Form(""),
    phone: str = Form(""),
    phone2: str = Form(""),
    fax: str = Form(""),
    address: str = Form(""),
    governorate: str = Form(""),
    city: str = Form(""),
    region: str = Form(""),
    email: str = Form(""),
    website: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    clean_name = clean_text(name)
    if supplier and clean_name:
        supplier.name = clean_name
        supplier.contact_name = clean_text(contact_name)
        supplier.contact_phone = clean_text(contact_phone)
        supplier.phone = clean_text(phone)
        supplier.phone2 = clean_text(phone2)
        supplier.fax = clean_text(fax)
        supplier.address = clean_text(address)
        supplier.governorate = clean_text(governorate)
        supplier.city = clean_text(city)
        supplier.region = clean_text(region)
        supplier.email = clean_text(email)
        supplier.website = clean_text(website)
        supplier.notes = clean_text(notes)
        db.commit()
    suppliers = db.query(Supplier).order_by(Supplier.id.desc()).all()
    return templates.TemplateResponse("suppliers/table.html", {"request": request, "suppliers": suppliers})


@app.delete("/suppliers/{supplier_id}", response_class=HTMLResponse)
async def delete_supplier(request: Request, supplier_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first()
    if supplier:
        db.delete(supplier)
        db.commit()
    suppliers = db.query(Supplier).order_by(Supplier.id.desc()).all()
    return templates.TemplateResponse("suppliers/table.html", {"request": request, "suppliers": suppliers})


@app.get("/suppliers/adjustments/table", response_class=HTMLResponse)
async def supplier_adjustments_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    adjustments = db.query(SupplierAdjustment).order_by(SupplierAdjustment.id.desc()).all()
    return templates.TemplateResponse(
        "suppliers/adjustments_table.html",
        {"request": request, "adjustments": adjustments},
    )


@app.post("/suppliers/adjustments", response_class=HTMLResponse)
async def create_supplier_adjustment(
    request: Request,
    supplier_id: str = Form(""),
    date: str = Form(""),
    adjustment_type: str = Form("discount"),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    clean_supplier_id = int(supplier_id) if supplier_id else None
    parsed_amount = parse_decimal(amount)
    if clean_supplier_id and parsed_amount > 0:
        adj = SupplierAdjustment(
            supplier_id=clean_supplier_id,
            date=parse_date_only(date),
            adjustment_type=adjustment_type if adjustment_type in {"discount", "addition"} else "discount",
            amount=parsed_amount,
            notes=clean_text(notes),
        )
        db.add(adj)
        db.flush()
        add_supplier_transaction(
            db,
            supplier_id=clean_supplier_id,
            date=datetime.combine(adj.date, datetime.min.time()),
            amount=_supplier_tx_amount(adj.adjustment_type, parsed_amount),
            tx_type=adj.adjustment_type,
            notes=adj.notes,
            source_type="supplier_adjustment",
            source_id=adj.id,
        )
        db.flush()
        rebuild_supplier_ledger_for_supplier(db, clean_supplier_id)
        db.commit()
    adjustments = db.query(SupplierAdjustment).order_by(SupplierAdjustment.id.desc()).all()
    return templates.TemplateResponse(
        "suppliers/adjustments_table.html",
        {"request": request, "adjustments": adjustments},
    )


@app.delete("/suppliers/adjustments/{adjustment_id}", response_class=HTMLResponse)
async def delete_supplier_adjustment(
    request: Request, adjustment_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    adjustment = db.query(SupplierAdjustment).filter(SupplierAdjustment.id == adjustment_id).first()
    if adjustment:
        db.query(SupplierTransaction).filter(
            SupplierTransaction.source_type == "supplier_adjustment",
            SupplierTransaction.source_id == adjustment.id,
        ).delete(synchronize_session=False)
        supplier_id = adjustment.supplier_id
        db.delete(adjustment)
        if supplier_id:
            rebuild_supplier_ledger_for_supplier(db, supplier_id)
        db.commit()
    adjustments = db.query(SupplierAdjustment).order_by(SupplierAdjustment.id.desc()).all()
    return templates.TemplateResponse(
        "suppliers/adjustments_table.html",
        {"request": request, "adjustments": adjustments},
    )

# -------------------------
# Purchases with lots
# -------------------------
@app.get("/purchases", response_class=HTMLResponse)
async def read_purchases(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    purchases = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase")
        .order_by(Purchase.id.desc())
        .all()
    )
    returns = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase_return")
        .order_by(Purchase.id.desc())
        .all()
    )
    orders = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    locations = (
        db.query(Location)
        .filter(Location.type.in_(["warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    items_json = json.dumps(items_data, ensure_ascii=False)
    supplier_balance_map = {s.id: get_supplier_balance(db, s.id) for s in suppliers}
    return templates.TemplateResponse(
        "purchases/page.html",
        {
            "request": request,
            "purchases": purchases,
            "returns": returns,
            "orders": orders,
            "suppliers": suppliers,
            "locations": locations,
            "items": items_data,
            "items_json": items_json,
            "supplier_balance_map": supplier_balance_map,
            "today": datetime.now().strftime("%Y-%m-%d"),
            "active_page": "purchases",
        },
    )


@app.get("/purchases/new", response_class=HTMLResponse)
async def new_purchase(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/purchases", status_code=303)


@app.post("/purchases/new", response_class=HTMLResponse)
async def create_purchase(
    request: Request,
    date: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    invoice_no: str = Form(""),
    notes: str = Form(""),
    shipping_cost: str = Form("0"),
    item_id: List[str] = Form([]),
    lot_code: List[str] = Form([]),
    expiry_month: List[str] = Form([]),
    qty: List[str] = Form([]),
    bonus_qty: List[str] = Form([]),
    tax_amount: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    discount_base: List[str] = Form([]),
    discount_extra: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    is_hx = request.headers.get("HX-Request") == "true"
    if not supplier_id:
        errors.append("برجاء اختيار المورد.")
    if not location_id:
        errors.append("برجاء اختيار المخزن.")
    if errors:
        purchases = (
            db.query(Purchase)
            .filter(Purchase.kind == "purchase")
            .order_by(Purchase.id.desc())
            .all()
        )
        suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
        locations = (
            db.query(Location)
            .filter(Location.type.in_(["warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
        items_json = json.dumps(items_data, ensure_ascii=False)
        supplier_balance_map = {s.id: get_supplier_balance(db, s.id) for s in suppliers}
        return templates.TemplateResponse(
            "purchases/sections/purchases_list.html",
            {
                "request": request,
                "purchases": purchases,
                "suppliers": suppliers,
                "locations": locations,
                "items": items_data,
                "items_json": items_json,
                "supplier_balance_map": supplier_balance_map,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )
    purchase = Purchase(
        date=parse_date(date),
        supplier_id=int(supplier_id),
        location_id=int(location_id),
        invoice_no=clean_text(invoice_no),
        notes=clean_text(notes),
        shipping_cost=parse_decimal(shipping_cost),
        subtotal=Decimal("0"),
        total=Decimal("0"),
        kind="purchase",
    )
    db.add(purchase)
    db.flush()

    subtotal = Decimal("0")
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        qty_val = parse_decimal(qty[idx] if idx < len(qty) else "0")
        bonus_val = parse_decimal(bonus_qty[idx] if idx < len(bonus_qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        tax_percent = parse_decimal(tax_amount[idx] if idx < len(tax_amount) else "0")
        disc_base = parse_decimal(discount_base[idx] if idx < len(discount_base) else "0")
        disc_extra = parse_decimal(discount_extra[idx] if idx < len(discount_extra) else "0")
        if qty_val <= 0:
            continue
        lot_val = clean_text(lot_code[idx] if idx < len(lot_code) else "")
        if not lot_val:
            continue
        expiry_val = parse_month_year(expiry_month[idx] if idx < len(expiry_month) else "")
        lot = get_or_create_lot(
            db,
            int(raw_item_id),
            lot_val,
            price_val,
            expiry_date=expiry_val,
        )
        base_total = qty_val * price_val
        if base_total < 0:
            base_total = Decimal("0")
        disc_percent = disc_base + disc_extra
        if disc_percent < 0:
            disc_percent = Decimal("0")
        if disc_percent > Decimal("100"):
            disc_percent = Decimal("100")
        base_total = base_total - (base_total * disc_percent) / Decimal("100")
        if base_total < 0:
            base_total = Decimal("0")
        tax_rate = tax_percent if tax_percent > 0 else Decimal("0")
        tax_val = (base_total * tax_rate) / Decimal("100")
        line_total = base_total + tax_val
        if line_total < 0:
            line_total = Decimal("0")
        subtotal += line_total
        purchase_item = PurchaseItem(
            purchase_id=purchase.id,
            item_id=int(raw_item_id),
            lot_id=lot.id,
            qty=qty_val,
            bonus_qty=bonus_val,
            tax_amount=tax_val,
            unit_price=price_val,
            discount_base=disc_base,
            discount_extra=disc_extra,
            total=line_total,
            line_total=line_total,
        )
        db.add(purchase_item)
        db.add(
            InventoryMove(
                date=purchase.date,
                location_id=int(location_id),
                item_id=int(raw_item_id),
                lot_id=lot.id,
                qty_in=qty_val + bonus_val,
                qty_out=Decimal("0"),
                source_type="purchase",
                source_id=purchase.id,
            )
        )

    purchase.subtotal = subtotal
    purchase.total = subtotal
    upsert_purchase_supplier_tx(db, purchase)
    sync_purchase_shipping_expense(db, purchase)
    db.commit()

    if is_hx:
        purchases = (
            db.query(Purchase)
            .filter(Purchase.kind == "purchase")
            .order_by(Purchase.id.desc())
            .all()
        )
        suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
        return templates.TemplateResponse(
            "purchases/table.html",
            {"request": request, "purchases": purchases, "suppliers": suppliers},
        )
    return RedirectResponse(url=f"/purchases/{purchase.id}", status_code=303)


@app.get("/purchases/{purchase_id}", response_class=HTMLResponse)
async def purchase_details(request: Request, purchase_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id).first()
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    paid_total = (
        db.query(func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
        .filter(PurchaseInvoicePayment.purchase_id == purchase_id)
        .scalar()
        or 0
    )
    paid_total = Decimal(str(paid_total))
    prev_balance = get_supplier_balance(
        db,
        purchase.supplier_id if purchase else None,
        up_to=purchase.date if purchase else None,
        exclude_purchase_id=purchase.id if purchase else None,
    )
    invoice_total = Decimal(str(purchase.total or 0)) if purchase else Decimal("0")
    new_balance = prev_balance + invoice_total
    return templates.TemplateResponse(
        "purchases/details.html",
        {
            "request": request,
            "purchase": purchase,
            "suppliers": suppliers,
            "prev_balance": prev_balance,
            "invoice_total": invoice_total,
            "new_balance": new_balance,
            "paid_total": paid_total,
            "remaining_total": invoice_total - paid_total,
            "active_page": "purchases",
        },
    )


@app.get("/purchases/table", response_class=HTMLResponse)
async def purchases_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    purchases = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase")
        .order_by(Purchase.id.desc())
        .all()
    )
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    return templates.TemplateResponse(
        "purchases/table.html", {"request": request, "purchases": purchases, "suppliers": suppliers}
    )


@app.post("/purchases/{purchase_id}/update", response_class=HTMLResponse)
async def update_purchase(
    request: Request,
    purchase_id: int,
    date: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    invoice_no: str = Form(""),
    shipping_cost: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id).first()
    if purchase:
        purchase.date = parse_date(date)
        purchase.supplier_id = int(supplier_id) if supplier_id else None
        purchase.location_id = int(location_id) if location_id else None
        purchase.invoice_no = clean_text(invoice_no)
        purchase.shipping_cost = parse_decimal(shipping_cost)
        purchase.notes = clean_text(notes)
        purchase.total = purchase.subtotal
        upsert_purchase_supplier_tx(db, purchase)
        sync_purchase_shipping_expense(db, purchase)
        db.commit()
    purchases = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase")
        .order_by(Purchase.id.desc())
        .all()
    )
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    return templates.TemplateResponse(
        "purchases/table.html", {"request": request, "purchases": purchases, "suppliers": suppliers}
    )


@app.post("/purchases/{purchase_id}/update-details", response_class=HTMLResponse)
async def update_purchase_details(
    request: Request,
    purchase_id: int,
    date: str = Form(""),
    supplier_id: str = Form(""),
    invoice_no: str = Form(""),
    shipping_cost: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id).first()
    if purchase:
        purchase.date = parse_date(date)
        purchase.supplier_id = int(supplier_id) if supplier_id else None
        purchase.invoice_no = clean_text(invoice_no)
        purchase.shipping_cost = parse_decimal(shipping_cost)
        purchase.notes = clean_text(notes)
        purchase.total = purchase.subtotal
        upsert_purchase_supplier_tx(db, purchase)
        sync_purchase_shipping_expense(db, purchase)
        db.commit()
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id).first()
    paid_total = (
        db.query(func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
        .filter(PurchaseInvoicePayment.purchase_id == purchase_id)
        .scalar()
        or 0
    )
    paid_total = Decimal(str(paid_total))
    prev_balance = get_supplier_balance(
        db,
        purchase.supplier_id if purchase else None,
        up_to=purchase.date if purchase else None,
        exclude_purchase_id=purchase.id if purchase else None,
    )
    invoice_total = Decimal(str(purchase.total or 0)) if purchase else Decimal("0")
    new_balance = prev_balance + invoice_total
    return templates.TemplateResponse(
        "purchases/details_content.html",
        {
            "request": request,
            "purchase": purchase,
            "suppliers": suppliers,
            "prev_balance": prev_balance,
            "invoice_total": invoice_total,
            "new_balance": new_balance,
            "paid_total": paid_total,
            "remaining_total": invoice_total - paid_total,
        },
    )


@app.post("/purchases/items/{line_id}/update", response_class=HTMLResponse)
async def update_purchase_line(
    request: Request,
    line_id: int,
    qty: str = Form("0"),
    unit_price: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    line = db.query(PurchaseItem).filter(PurchaseItem.id == line_id).first()
    if line:
        qty_val = parse_decimal(qty)
        price_val = parse_decimal(unit_price)
        if qty_val > 0 and price_val >= 0:
            line.qty = qty_val
            line.unit_price = price_val
            line.total = qty_val * price_val
            purchase = db.query(Purchase).filter(Purchase.id == line.purchase_id).first()
            if purchase:
                purchase.subtotal = sum((li.total or 0) for li in purchase.items)
                purchase.total = purchase.subtotal
            move = (
                db.query(InventoryMove)
                .filter(
                    InventoryMove.source_type == "purchase",
                    InventoryMove.source_id == line.purchase_id,
                    InventoryMove.item_id == line.item_id,
                    InventoryMove.lot_id == line.lot_id,
                )
                .first()
            )
            if move:
                move.qty_in = qty_val
            if purchase:
                upsert_purchase_supplier_tx(db, purchase)
            db.commit()
    purchase = db.query(Purchase).filter(Purchase.id == line.purchase_id).first() if line else None
    return templates.TemplateResponse(
        "purchases/lines_table.html",
        {"request": request, "purchase": purchase},
    )


@app.get("/purchases/{purchase_id}/print", response_class=HTMLResponse)
async def purchase_print(request: Request, purchase_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    purchase = db.query(Purchase).filter(Purchase.id == purchase_id).first()
    settings = get_print_settings()
    paid_total = (
        db.query(func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
        .filter(PurchaseInvoicePayment.purchase_id == purchase_id)
        .scalar()
        or 0
    )
    paid_total = Decimal(str(paid_total))
    prev_balance = get_supplier_balance(
        db, purchase.supplier_id if purchase else None, up_to=purchase.date if purchase else None, exclude_purchase_id=purchase.id if purchase else None
    )
    invoice_total = Decimal(str(purchase.total or 0)) if purchase else Decimal("0")
    new_balance = prev_balance + invoice_total
    return templates.TemplateResponse(
        "purchases/print.html",
        {
            "request": request,
            "purchase": purchase,
            "settings": settings,
            "prev_balance": prev_balance,
            "invoice_total": invoice_total,
            "new_balance": new_balance,
            "paid_total": paid_total,
            "remaining_total": invoice_total - paid_total,
        },
    )


# -------------------------
# Transfers (Distribution)
# -------------------------
@app.get("/transfers", response_class=HTMLResponse)
async def read_transfers(request: Request,user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/items", status_code=303)


@app.get("/transfers/new", response_class=HTMLResponse)
async def new_transfer(request: Request,user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/items", status_code=303)


@app.post("/transfers/new", response_class=HTMLResponse)
async def create_transfer(
    request: Request,
    date: str = Form(""),
    from_location_id: str = Form(""),
    to_location_id: str = Form(""),
    rep_id: str = Form(""),
    price_category: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    requested_qty: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    doctor_id: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    is_hx = request.headers.get("HX-Request") == "true"
    if not from_location_id or not to_location_id:
        errors.append("برجاء اختيار مخزن التحويل والجهة المستلمة.")
    if from_location_id == to_location_id:
        errors.append("لا يمكن التحويل لنفس المخزن.")
    lines = []
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        qty_val = parse_decimal(requested_qty[idx] if idx < len(requested_qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        if qty_val <= 0:
            errors.append("يرجى إدخال كمية صحيحة.")
            continue
        if price_val < 0:
            errors.append("سعر الصنف لا يمكن أن يكون سالباً.")
            continue
        doc_ref = doctor_id[idx] if idx < len(doctor_id) else ""
        lines.append(
            {
                "item_id": int(raw_item_id),
                "requested_qty": qty_val,
                "unit_price": price_val,
                "doctor_id": int(doc_ref) if doc_ref else None,
            }
        )
    if not lines:
        errors.append("يرجى إدخال أصناف للتحويل.")

    if errors:
        locations = db.query(Location).order_by(Location.name.asc()).all()
        items = db.query(Item).order_by(Item.name.asc()).all()
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        items_data = [
            {"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items
        ]
        doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
        if is_hx:
            return templates.TemplateResponse(
                "items/sections/transfers_new.html",
                {
                    "request": request,
                    "locations": locations,
                    "items": items_data,
                    "doctors": doctors_data,
                    "reps": reps,
                    "today": datetime.now().strftime("%Y-%m-%d"),
                    "errors": errors,
                },
            )
        return templates.TemplateResponse(
            "transfers/create.html",
            {
                "request": request,
                "locations": locations,
                "items": items_data,
                "doctors": doctors_data,
                "reps": reps,
                "active_page": "transfers",
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    transfer_date = parse_date(date)
    transfer = Transfer(
        date=transfer_date,
        kind="transfer",
        price_category=clean_text(price_category),
        from_location_id=int(from_location_id),
        to_location_id=int(to_location_id),
        rep_id=int(rep_id) if rep_id else None,
        notes=clean_text(notes),
        total=Decimal("0"),
    )
    db.add(transfer)
    db.flush()

    total = Decimal("0")
    to_location = db.query(Location).filter(Location.id == int(to_location_id)).first()
    for line in lines:
        if to_location and to_location.type != "pharmacy":
            line["doctor_id"] = None
        remaining = line["requested_qty"]
        allocations = []
        available_lots = get_available_lots(db, int(from_location_id), line["item_id"])
        for lot, available in available_lots:
            if remaining <= 0:
                break
            take_qty = min(remaining, available)
            if take_qty <= 0:
                continue
            allocations.append((lot, take_qty))
            remaining -= take_qty
        if remaining > 0:
            item_name = db.query(Item.name).filter(Item.id == line["item_id"]).scalar() or "صنف غير معروف"
            errors.append(f"الكمية المتاحة غير كافية للصنف: {item_name}.")
            break

        line_total = line["requested_qty"] * line["unit_price"]
        total += line_total
        transfer_line = TransferLine(
            transfer_id=transfer.id,
            item_id=line["item_id"],
            requested_qty=line["requested_qty"],
            unit_price=line["unit_price"],
            doctor_id=line["doctor_id"],
            commission_amount=Decimal("0"),
            bonus_amount=Decimal("0"),
            line_total=line_total,
        )
        db.add(transfer_line)
        db.flush()

        for lot, qty_val in allocations:
            db.add(
                TransferAllocation(
                    transfer_line_id=transfer_line.id,
                    lot_id=lot.id,
                    qty=qty_val,
                    lot_code_snapshot=lot.lot_code,
                )
            )
            db.add(
                InventoryMove(
                    date=transfer_date,
                    location_id=int(from_location_id),
                    item_id=line["item_id"],
                    lot_id=lot.id,
                    qty_in=Decimal("0"),
                    qty_out=qty_val,
                    source_type="transfer",
                    source_id=transfer.id,
                )
            )
            db.add(
                InventoryMove(
                    date=transfer_date,
                    location_id=int(to_location_id),
                    item_id=line["item_id"],
                    lot_id=lot.id,
                    qty_in=qty_val,
                    qty_out=Decimal("0"),
                    source_type="transfer",
                    source_id=transfer.id,
                )
            )

        if to_location and to_location.type == "pharmacy" and line["doctor_id"]:
            rule = find_commission_rule(
                db, line["doctor_id"], int(to_location_id), line["item_id"], transfer_date.date()
            )
            if rule:
                commission = Decimal("0")
                for lot, qty_val in allocations:
                    if rule.commission_type == "percent":
                        commission += qty_val * line["unit_price"] * (rule.commission_value / Decimal("100"))
                    else:
                        commission += qty_val * rule.commission_value
                transfer_line.commission_amount = commission
                db.add(
                    DoctorTransaction(
                        date=transfer_date,
                        doctor_id=line["doctor_id"],
                        pharmacy_location_id=int(to_location_id),
                        transfer_id=transfer.id,
                        type="commission_earned",
                        amount=commission,
                        notes="عمولة طبيب",
                    )
                )

    if errors:
        db.rollback()
        locations = db.query(Location).order_by(Location.name.asc()).all()
        items = db.query(Item).order_by(Item.name.asc()).all()
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        items_data = [
            {"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items
        ]
        doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
        return templates.TemplateResponse(
            "transfers/create.html",
            {
                "request": request,
                "locations": locations,
                "items": items_data,
                "doctors": doctors_data,
                "reps": reps,
                "active_page": "transfers",
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    transfer.total = total
    if to_location and to_location.type == "pharmacy":
        db.add(
            LocationTransaction(
                date=transfer_date,
                location_id=int(to_location_id),
                type="invoice",
                amount=total,
                notes="فاتورة تحويل",
                source_type="transfer",
                source_id=transfer.id,
            )
        )
    db.commit()
    if is_hx:
        transfers = db.query(Transfer).order_by(Transfer.id.desc()).all()
        return templates.TemplateResponse(
            "items/sections/transfers.html",
            {"request": request, "transfers": transfers},
        )
    return RedirectResponse(url=f"/transfers/{transfer.id}", status_code=303)


@app.get("/transfers/{transfer_id}", response_class=HTMLResponse)
async def transfer_details(request: Request, transfer_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    transfer = db.query(Transfer).filter(Transfer.id == transfer_id).first()
    return templates.TemplateResponse(
        "transfers/details.html",
        {"request": request, "transfer": transfer, "active_page": "transfers"},
    )


@app.get("/transfers/{transfer_id}/print", response_class=HTMLResponse)
async def transfer_print(request: Request, transfer_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    transfer = db.query(Transfer).filter(Transfer.id == transfer_id).first()
    settings = get_print_settings()
    return templates.TemplateResponse(
        "transfers/print.html", {"request": request, "transfer": transfer, "settings": settings}
    )


@app.delete("/transfers/{transfer_id}", response_class=HTMLResponse)
async def delete_transfer(request: Request, transfer_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    transfer = db.query(Transfer).filter(Transfer.id == transfer_id).first()
    if transfer:
        db.query(DoctorTransaction).filter(DoctorTransaction.transfer_id == transfer_id).delete()
        db.query(LocationTransaction).filter(
            LocationTransaction.source_type == "transfer",
            LocationTransaction.source_id == transfer_id,
        ).delete()
        db.query(InventoryMove).filter(
            InventoryMove.source_type == "transfer", InventoryMove.source_id == transfer_id
        ).delete()
        db.delete(transfer)
        db.commit()
    transfers = (
        db.query(Transfer)
        .filter(Transfer.kind == "transfer")
        .order_by(Transfer.id.desc())
        .all()
    )
    return templates.TemplateResponse(
        "transfers/table.html", {"request": request, "transfers": transfers}
    )


@app.get("/transfers/table", response_class=HTMLResponse)
async def read_transfers_table(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    transfers = (
        db.query(Transfer)
        .filter(Transfer.kind == "transfer")
        .order_by(Transfer.id.desc())
        .all()
    )
    return templates.TemplateResponse(
        "transfers/table.html", {"request": request, "transfers": transfers}
    )

# -------------------------
# Sales
# -------------------------
@app.get("/sales", response_class=HTMLResponse)
async def read_sales(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    sales = db.query(Transfer).filter(Transfer.kind == "sale").order_by(Transfer.id.desc()).all()
    returns = (
        db.query(Transfer)
        .filter(Transfer.kind == "sale_return")
        .order_by(Transfer.id.desc())
        .all()
    )
    orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
    customers = (
        db.query(Location)
        .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
    doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
    items_json = json.dumps(items_data, ensure_ascii=False)
    customer_balance_map = {c.id: get_location_balance(db, c.id) for c in customers}
    doctors_json = json.dumps(doctors_data, ensure_ascii=False)
    customer_balance_map = {c.id: get_location_balance(db, c.id) for c in customers}
    return templates.TemplateResponse(
        "sales/page.html",
        {
            "request": request,
            "sales": sales,
            "returns": returns,
            "orders": orders,
            "customers": customers,
            "items": items_data,
            "items_json": items_json,
            "doctors_json": doctors_json,
            "customer_balance_map": customer_balance_map,
            "reps": reps,
            "main_warehouse": get_main_warehouse(db),
            "today": datetime.now().strftime("%Y-%m-%d"),
            "active_page": "sales",
        },
    )

@app.get("/purchases/section/{section}", response_class=HTMLResponse)
async def purchases_section(request: Request, section: str, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    purchases = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase")
        .order_by(Purchase.id.desc())
        .all()
    )
    returns = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase_return")
        .order_by(Purchase.id.desc())
        .all()
    )
    orders = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    locations = (
        db.query(Location)
        .filter(Location.type.in_(["warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    items_json = json.dumps(items_data, ensure_ascii=False)
    supplier_balance_map = {s.id: get_supplier_balance(db, s.id) for s in suppliers}
    context_base = {
        "request": request,
        "suppliers": suppliers,
        "locations": locations,
        "items": items_data,
        "items_json": items_json,
        "supplier_balance_map": supplier_balance_map,
        "today": datetime.now().strftime("%Y-%m-%d"),
    }
    if section == "returns":
        return templates.TemplateResponse(
            "purchases/sections/returns.html",
            {**context_base, "returns": returns},
        )
    if section == "orders":
        return templates.TemplateResponse(
            "purchases/sections/orders.html",
            {**context_base, "orders": orders},
        )
    if section == "opening":
        opening_rows = (
            db.query(SupplierAdjustment.supplier_id, func.coalesce(func.sum(SupplierAdjustment.amount), 0))
            .filter(SupplierAdjustment.adjustment_type == "opening_balance")
            .group_by(SupplierAdjustment.supplier_id)
            .all()
        )
        opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
        return templates.TemplateResponse(
            "purchases/sections/opening.html",
            {**context_base, "suppliers": suppliers, "opening_map": opening_map},
        )
    if section == "other-expenses":
        expenses = db.query(OtherExpense).order_by(OtherExpense.id.desc()).all()
        return templates.TemplateResponse(
            "purchases/sections/other_expenses.html",
            {**context_base, "expenses": expenses},
        )
    if section == "reports":
        return templates.TemplateResponse(
            "purchases/sections/reports.html",
            {"request": request},
        )
    return templates.TemplateResponse(
        "purchases/sections/purchases_list.html",
        {**context_base, "purchases": purchases},
    )


@app.post("/purchases/other-expenses", response_class=HTMLResponse)
async def create_other_expense(
    request: Request,
    title: str = Form(""),
    amount: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    title_val = clean_text(title)
    amount_val = parse_decimal(amount)
    if not title_val:
        errors.append("يرجى إدخال البيان.")
    if amount_val <= 0:
        errors.append("يرجى إدخال مبلغ صحيح.")
    if not errors:
        main_cash = get_main_cash_account(db)
        expense = OtherExpense(
            date=datetime.now(),
            title=title_val,
            amount=amount_val,
            notes=None,
            source_type="other_expense",
            source_id=None,
        )
        db.add(expense)
        db.flush()
        db.add(
            CashTransaction(
                date=expense.date,
                type="other_expense",
                amount=amount_val,
                from_account_id=main_cash.id,
                to_account_id=None,
                notes=title_val,
                source_type="other_expense",
                source_id=expense.id,
            )
        )
        db.commit()

    expenses = db.query(OtherExpense).order_by(OtherExpense.id.desc()).all()
    return templates.TemplateResponse(
        "purchases/other_expenses_table.html",
        {"request": request, "expenses": expenses, "errors": errors},
    )


@app.post("/purchases/returns/new", response_class=HTMLResponse)
async def create_purchase_return(
    request: Request,
    date: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    invoice_no: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    lot_code: List[str] = Form([]),
    qty: List[str] = Form([]),
    bonus_qty: List[str] = Form([]),
    tax_amount: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    discount_base: List[str] = Form([]),
    discount_extra: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    is_hx = request.headers.get("HX-Request") == "true"
    if not supplier_id:
        errors.append("برجاء اختيار المورد.")
    if not location_id:
        errors.append("برجاء اختيار المخزن.")

    returns = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase_return")
        .order_by(Purchase.id.desc())
        .all()
    )
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    locations = (
        db.query(Location)
        .filter(Location.type.in_(["warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    items_json = json.dumps(items_data, ensure_ascii=False)

    if errors:
        return templates.TemplateResponse(
            "purchases/sections/returns.html",
            {
                "request": request,
                "returns": returns,
                "suppliers": suppliers,
                "locations": locations,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    purchase = Purchase(
        date=parse_date(date),
        supplier_id=int(supplier_id),
        location_id=int(location_id),
        invoice_no=clean_text(invoice_no),
        notes=clean_text(notes),
        shipping_cost=Decimal("0"),
        subtotal=Decimal("0"),
        total=Decimal("0"),
        kind="purchase_return",
    )
    db.add(purchase)
    db.flush()

    subtotal = Decimal("0")
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        qty_val = parse_decimal(qty[idx] if idx < len(qty) else "0")
        bonus_val = parse_decimal(bonus_qty[idx] if idx < len(bonus_qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        tax_percent = parse_decimal(tax_amount[idx] if idx < len(tax_amount) else "0")
        disc_base = parse_decimal(discount_base[idx] if idx < len(discount_base) else "0")
        disc_extra = parse_decimal(discount_extra[idx] if idx < len(discount_extra) else "0")
        if qty_val <= 0:
            continue
        lot_val = clean_text(lot_code[idx] if idx < len(lot_code) else "")
        if not lot_val:
            errors.append("يرجى إدخال رقم التشغيلة.")
            continue
        lot = (
            db.query(ItemLot)
            .filter(ItemLot.item_id == int(raw_item_id), ItemLot.lot_code == lot_val)
            .first()
        )
        if not lot:
            errors.append(f"لا توجد تشغيلة بهذا الكود: {lot_val}.")
            continue
        available = get_lot_balance(db, int(location_id), lot.id)
        if available < (qty_val + bonus_val):
            errors.append("الكمية المطلوبة أكبر من الرصيد المتاح.")
            continue
        base_total = qty_val * price_val
        if base_total < 0:
            base_total = Decimal("0")
        disc_percent = disc_base + disc_extra
        if disc_percent < 0:
            disc_percent = Decimal("0")
        if disc_percent > Decimal("100"):
            disc_percent = Decimal("100")
        base_total = base_total - (base_total * disc_percent) / Decimal("100")
        if base_total < 0:
            base_total = Decimal("0")
        tax_rate = tax_percent if tax_percent > 0 else Decimal("0")
        tax_val = (base_total * tax_rate) / Decimal("100")
        line_total = base_total + tax_val
        if line_total < 0:
            line_total = Decimal("0")
        subtotal += line_total
        db.add(
            PurchaseItem(
                purchase_id=purchase.id,
                item_id=int(raw_item_id),
                lot_id=lot.id,
                qty=qty_val,
                bonus_qty=bonus_val,
                tax_amount=tax_val,
                unit_price=price_val,
                discount_base=disc_base,
                discount_extra=disc_extra,
                total=line_total,
                line_total=line_total,
            )
        )
        db.add(
            InventoryMove(
                date=purchase.date,
                location_id=int(location_id),
                item_id=int(raw_item_id),
                lot_id=lot.id,
                qty_in=Decimal("0"),
                qty_out=qty_val + bonus_val,
                source_type="purchase_return",
                source_id=purchase.id,
            )
        )

    if errors:
        db.rollback()
        return templates.TemplateResponse(
            "purchases/sections/returns.html",
            {
                "request": request,
                "returns": returns,
                "suppliers": suppliers,
                "locations": locations,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    purchase.subtotal = subtotal
    purchase.total = subtotal
    upsert_purchase_supplier_tx(db, purchase)
    db.commit()

    if is_hx:
        returns = (
            db.query(Purchase)
            .filter(Purchase.kind == "purchase_return")
            .order_by(Purchase.id.desc())
            .all()
        )
        return templates.TemplateResponse(
            "purchases/returns_table.html",
            {"request": request, "returns": returns},
        )
    return RedirectResponse(url=f"/purchases/{purchase.id}", status_code=303)


@app.post("/purchases/orders", response_class=HTMLResponse)
async def create_purchase_order(
    request: Request,
    date: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    lot_code: List[str] = Form([]),
    qty: List[str] = Form([]),
    bonus_qty: List[str] = Form([]),
    tax_amount: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    discount_base: List[str] = Form([]),
    discount_extra: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    clean_supplier_id = int(supplier_id) if supplier_id else None
    clean_location_id = int(location_id) if location_id else None
    if not clean_supplier_id:
        errors.append("برجاء اختيار المورد.")
    if not clean_location_id:
        errors.append("برجاء اختيار المخزن.")

    orders = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    locations = (
        db.query(Location)
        .filter(Location.type.in_(["warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    items_json = json.dumps(items_data, ensure_ascii=False)

    if errors:
        return templates.TemplateResponse(
            "purchases/sections/orders.html",
            {
                "request": request,
                "orders": orders,
                "suppliers": suppliers,
                "locations": locations,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    order = PurchaseOrder(
        date=parse_date(date),
        supplier_id=clean_supplier_id,
        location_id=clean_location_id,
        notes=clean_text(notes),
        status="open",
        total=Decimal("0"),
    )
    db.add(order)
    db.flush()

    total = Decimal("0")
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        qty_val = parse_decimal(qty[idx] if idx < len(qty) else "0")
        bonus_val = parse_decimal(bonus_qty[idx] if idx < len(bonus_qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        tax_percent = parse_decimal(tax_amount[idx] if idx < len(tax_amount) else "0")
        disc_base = parse_decimal(discount_base[idx] if idx < len(discount_base) else "0")
        disc_extra = parse_decimal(discount_extra[idx] if idx < len(discount_extra) else "0")
        if qty_val <= 0:
            continue
        base_total = qty_val * price_val
        if base_total < 0:
            base_total = Decimal("0")
        disc_percent = disc_base + disc_extra
        if disc_percent < 0:
            disc_percent = Decimal("0")
        if disc_percent > Decimal("100"):
            disc_percent = Decimal("100")
        base_total = base_total - (base_total * disc_percent) / Decimal("100")
        if base_total < 0:
            base_total = Decimal("0")
        tax_rate = tax_percent if tax_percent > 0 else Decimal("0")
        tax_val = (base_total * tax_rate) / Decimal("100")
        line_total = base_total + tax_val
        if line_total < 0:
            line_total = Decimal("0")
        total += line_total
        db.add(
            PurchaseOrderLine(
                order_id=order.id,
                item_id=int(raw_item_id),
                lot_code=clean_text(lot_code[idx] if idx < len(lot_code) else ""),
                qty=qty_val,
                bonus_qty=bonus_val,
                tax_amount=tax_val,
                unit_price=price_val,
                discount_base=disc_base,
                discount_extra=disc_extra,
                line_total=line_total,
            )
        )

    order.total = total
    db.commit()
    orders = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    return templates.TemplateResponse(
        "purchases/orders_table.html",
        {"request": request, "orders": orders},
    )


@app.post("/purchases/opening", response_class=HTMLResponse)
async def set_supplier_opening_balance(
    request: Request,
    supplier_id: str = Form(...),
    amount: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    clean_id = int(supplier_id)
    existing_openings = (
        db.query(SupplierAdjustment)
        .filter(
            SupplierAdjustment.supplier_id == clean_id,
            SupplierAdjustment.adjustment_type == "opening_balance",
        )
        .all()
    )
    if existing_openings:
        opening_ids = [row.id for row in existing_openings]
        db.query(SupplierTransaction).filter(
            SupplierTransaction.source_type == "supplier_adjustment",
            SupplierTransaction.source_id.in_(opening_ids),
        ).delete(synchronize_session=False)
    db.query(SupplierAdjustment).filter(
        SupplierAdjustment.supplier_id == clean_id,
        SupplierAdjustment.adjustment_type == "opening_balance",
    ).delete()
    if amt != 0:
        opening = SupplierAdjustment(
            supplier_id=clean_id,
            date=parse_date_only(datetime.now().strftime("%Y-%m-%d")),
            adjustment_type="opening_balance",
            amount=amt,
            notes="رصيد أول المدة",
        )
        db.add(opening)
        db.flush()
        add_supplier_transaction(
            db,
            supplier_id=clean_id,
            date=datetime.combine(opening.date, datetime.min.time()),
            amount=amt,
            tx_type="opening_balance",
            notes=opening.notes,
            source_type="supplier_adjustment",
            source_id=opening.id,
        )
        db.flush()
    if clean_id:
        rebuild_supplier_ledger_for_supplier(db, clean_id)
    db.commit()
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    opening_rows = (
        db.query(SupplierAdjustment.supplier_id, func.coalesce(func.sum(SupplierAdjustment.amount), 0))
        .filter(SupplierAdjustment.adjustment_type == "opening_balance")
        .group_by(SupplierAdjustment.supplier_id)
        .all()
    )
    opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
    return templates.TemplateResponse(
        "purchases/opening_table.html",
        {"request": request, "suppliers": suppliers, "opening_map": opening_map},
    )


@app.delete("/purchases/orders/{order_id}", response_class=HTMLResponse)
async def delete_purchase_order(request: Request, order_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    order = db.query(PurchaseOrder).filter(PurchaseOrder.id == order_id).first()
    if order:
        db.delete(order)
        db.commit()
    orders = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    return templates.TemplateResponse(
        "purchases/orders_table.html",
        {"request": request, "orders": orders},
    )


@app.post("/purchases/orders/{order_id}/convert", response_class=HTMLResponse)
async def convert_purchase_order(
    request: Request,
    order_id: int,
    shipping_cost: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    order = db.query(PurchaseOrder).filter(PurchaseOrder.id == order_id).first()
    if not order:
        errors.append("الطلب غير موجود.")
    elif order.status == "converted":
        errors.append("الطلب تم تحويله مسبقاً.")

    orders = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    locations = (
        db.query(Location)
        .filter(Location.type.in_(["warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "purchase_price": float(i.purchase_price or 0)} for i in items]
    items_json = json.dumps(items_data, ensure_ascii=False)

    if errors or not order:
        return templates.TemplateResponse(
            "purchases/sections/orders.html",
            {
                "request": request,
                "orders": orders,
                "suppliers": suppliers,
                "locations": locations,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors or ["حدث خطأ غير متوقع."],
            },
        )

    purchase = Purchase(
        date=order.date,
        supplier_id=order.supplier_id,
        location_id=order.location_id,
        invoice_no=None,
        notes=f"تحويل من أمر شراء #{order.id}",
        shipping_cost=Decimal("0"),
        subtotal=Decimal("0"),
        total=Decimal("0"),
        kind="purchase",
    )
    db.add(purchase)
    db.flush()

    subtotal = Decimal("0")
    for line in order.lines:
        lot_val = clean_text(line.lot_code or "")
        if not lot_val:
            errors.append("لا توجد تشغيلة في أحد أصناف الطلب.")
            break
        lot = get_or_create_lot(db, line.item_id, lot_val, line.unit_price)
        line_total = Decimal(str(line.line_total or 0))
        subtotal += line_total
        db.add(
            PurchaseItem(
                purchase_id=purchase.id,
                item_id=line.item_id,
                lot_id=lot.id,
                qty=line.qty,
                bonus_qty=line.bonus_qty,
                tax_amount=line.tax_amount,
                unit_price=line.unit_price,
                discount_base=line.discount_base,
                discount_extra=line.discount_extra,
                total=line_total,
                line_total=line_total,
            )
        )
        db.add(
            InventoryMove(
                date=purchase.date,
                location_id=order.location_id,
                item_id=line.item_id,
                lot_id=lot.id,
                qty_in=Decimal(str(line.qty or 0)) + Decimal(str(line.bonus_qty or 0)),
                qty_out=Decimal("0"),
                source_type="purchase",
                source_id=purchase.id,
            )
        )

    if errors:
        db.rollback()
        return templates.TemplateResponse(
            "purchases/sections/orders.html",
            {
                "request": request,
                "orders": orders,
                "suppliers": suppliers,
                "locations": locations,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    purchase.subtotal = subtotal
    purchase.total = subtotal
    ship_val = parse_decimal(shipping_cost)
    purchase.shipping_cost = ship_val if ship_val > 0 else Decimal("0")
    order.status = "converted"
    upsert_purchase_supplier_tx(db, purchase)
    sync_purchase_shipping_expense(db, purchase)
    db.commit()
    orders = db.query(PurchaseOrder).order_by(PurchaseOrder.id.desc()).all()
    return templates.TemplateResponse(
        "purchases/sections/orders.html",
        {
            "request": request,
            "orders": orders,
            "suppliers": suppliers,
            "locations": locations,
            "items": items_data,
            "items_json": items_json,
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )


@app.get("/sales/section/{section}", response_class=HTMLResponse)
async def sales_section(request: Request, section: str, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    customers = (
        db.query(Location)
        .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
    doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
    items_json = json.dumps(items_data, ensure_ascii=False)
    customer_balance_map = {c.id: get_location_balance(db, c.id) for c in customers}
    if section == "returns":
        returns = (
            db.query(Transfer)
            .filter(Transfer.kind == "sale_return")
            .order_by(Transfer.id.desc())
            .all()
        )
        return templates.TemplateResponse(
            "sales/sections/returns.html",
            {
                "request": request,
                "returns": returns,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "main_warehouse": get_main_warehouse(db),
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    if section == "orders":
        orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
        return templates.TemplateResponse(
            "sales/sections/orders.html",
            {
                "request": request,
                "orders": orders,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    if section == "sales":
        sales = db.query(Transfer).filter(Transfer.kind == "sale").order_by(Transfer.id.desc()).all()
        return templates.TemplateResponse(
            "sales/sections/sales_list.html",
            {
                "request": request,
                "sales": sales,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "customer_balance_map": customer_balance_map,
                "reps": reps,
                "main_warehouse": get_main_warehouse(db),
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    if section == "opening":
        opening_rows = (
            db.query(LocationTransaction.location_id, func.coalesce(func.sum(LocationTransaction.amount), 0))
            .filter(LocationTransaction.type == "opening_balance")
            .group_by(LocationTransaction.location_id)
            .all()
        )
        opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
        return templates.TemplateResponse(
            "sales/sections/opening.html",
            {
                "request": request,
                "customers": customers,
                "opening_map": opening_map,
            },
        )
    if section == "reports":
        return templates.TemplateResponse(
            "sales/sections/reports.html",
            {"request": request},
        )
    sales = db.query(Transfer).filter(Transfer.kind == "sale").order_by(Transfer.id.desc()).all()
    return templates.TemplateResponse(
        "sales/sections/sales_list.html",
        {
            "request": request,
            "sales": sales,
            "customers": customers,
            "items": items_data,
            "items_json": items_json,
            "customer_balance_map": customer_balance_map,
            "reps": reps,
            "main_warehouse": get_main_warehouse(db),
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )


@app.post("/sales/new", response_class=HTMLResponse)
async def create_sale(
    request: Request,
    date: str = Form(""),
    to_location_id: str = Form(""),
    rep_id: str = Form(""),
    price_category: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    doctor_id: List[str] = Form([]),
    requested_qty: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    bonus_amount: List[str] = Form([]),
    discount_amount: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    is_hx = request.headers.get("HX-Request") == "true"
    main_wh = get_main_warehouse(db)
    if not to_location_id:
        errors.append("برجاء اختيار العميل.")
    to_location = db.query(Location).filter(Location.id == int(to_location_id or 0)).first()
    if not to_location or to_location.type not in {"pharmacy", "warehouse", "sub_warehouse"}:
        errors.append("الجهة غير صالحة أو ليست صيدلية/مخزن.")
    lines = []
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        qty_val = parse_decimal(requested_qty[idx] if idx < len(requested_qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        bonus_val = parse_decimal(bonus_amount[idx] if idx < len(bonus_amount) else "0")
        discount_val = parse_decimal(discount_amount[idx] if idx < len(discount_amount) else "0")
        raw_doctor = doctor_id[idx] if idx < len(doctor_id) else ""
        doctor_val = int(raw_doctor) if str(raw_doctor).isdigit() else None
        if qty_val <= 0:
            errors.append("يرجى إدخال كمية صحيحة.")
            continue
        if price_val < 0:
            errors.append("سعر الصنف لا يمكن أن يكون سالباً.")
            continue
        if bonus_val < 0:
            errors.append("البونص لا يمكن أن يكون سالباً.")
            continue
        if discount_val < 0:
            errors.append("الخصم لا يمكن أن يكون سالباً.")
            continue
        if discount_val > 100:
            errors.append("الخصم لا يمكن أن يتجاوز 100%.")
            continue
        lines.append(
            {
                "item_id": int(raw_item_id),
                "doctor_id": doctor_val,
                "requested_qty": qty_val,
                "unit_price": price_val,
                "bonus_amount": bonus_val,
                "discount_amount": discount_val,
            }
        )
    if not lines:
        errors.append("يرجى إدخال أصناف للبيع.")

    if errors:
        sales = db.query(Transfer).filter(Transfer.kind == "sale").order_by(Transfer.id.desc()).all()
        customers = (
            db.query(Location)
            .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
        doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
        items_json = json.dumps(items_data, ensure_ascii=False)
        doctors_json = json.dumps(doctors_data, ensure_ascii=False)
        customer_balance_map = {c.id: get_location_balance(db, c.id) for c in customers}
        return templates.TemplateResponse(
            "sales/sections/sales_list.html",
            {
                "request": request,
                "sales": sales,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "doctors_json": doctors_json,
                "customer_balance_map": customer_balance_map,
                "reps": reps,
                "main_warehouse": main_wh,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    sale_date = parse_date(date)
    sale = Transfer(
        date=sale_date,
        kind="sale",
        price_category=clean_text(price_category),
        from_location_id=main_wh.id,
        to_location_id=int(to_location_id),
        rep_id=int(rep_id) if rep_id else None,
        notes=clean_text(notes),
        total=Decimal("0"),
    )
    db.add(sale)
    db.flush()

    total = Decimal("0")
    bonus_allocations_all = []
    to_location = db.query(Location).filter(Location.id == int(to_location_id)).first()
    for line in lines:
        if to_location and to_location.type != "pharmacy":
            line["doctor_id"] = None
        remaining = line["requested_qty"] + line["bonus_amount"]
        allocations = []
        available_lots = get_available_lots(db, main_wh.id, line["item_id"])
        for lot, available in available_lots:
            if remaining <= 0:
                break
            take_qty = min(remaining, available)
            if take_qty <= 0:
                continue
            allocations.append((lot, take_qty))
            remaining -= take_qty
        if remaining > 0:
            item_name = db.query(Item.name).filter(Item.id == line["item_id"]).scalar() or "صنف غير معروف"
            errors.append(f"الكمية المتاحة غير كافية للصنف: {item_name} (شاملة البونص).")
            break

        line_base = line["requested_qty"] * line["unit_price"]
        line_discount = (line_base * line["discount_amount"]) / Decimal("100")
        line_total = line_base - line_discount
        if line_total < 0:
            line_total = Decimal("0")
        total += line_total
        sale_line = TransferLine(
            transfer_id=sale.id,
            item_id=line["item_id"],
            requested_qty=line["requested_qty"],
            unit_price=line["unit_price"],
            doctor_id=line["doctor_id"],
            commission_amount=Decimal("0"),
            bonus_amount=line["bonus_amount"],
            discount_amount=line["discount_amount"],
            line_total=line_total,
        )
        db.add(sale_line)
        db.flush()

        sale_remaining = line["requested_qty"]
        bonus_remaining = line["bonus_amount"]
        sale_allocations = []
        for lot, qty_val in allocations:
            if sale_remaining > 0:
                sale_qty = min(qty_val, sale_remaining)
                if sale_qty > 0:
                    sale_allocations.append((lot, sale_qty))
                    sale_remaining -= sale_qty
                    qty_val -= sale_qty
                    db.add(
                        TransferAllocation(
                            transfer_line_id=sale_line.id,
                            lot_id=lot.id,
                            qty=sale_qty,
                            lot_code_snapshot=lot.lot_code,
                        )
                    )
                    db.add(
                        InventoryMove(
                            date=sale_date,
                            location_id=main_wh.id,
                            item_id=line["item_id"],
                            lot_id=lot.id,
                            qty_in=Decimal("0"),
                            qty_out=sale_qty,
                            source_type="sale",
                            source_id=sale.id,
                        )
                    )
                    db.add(
                        InventoryMove(
                            date=sale_date,
                            location_id=int(to_location_id),
                            item_id=line["item_id"],
                            lot_id=lot.id,
                            qty_in=sale_qty,
                            qty_out=Decimal("0"),
                            source_type="sale",
                            source_id=sale.id,
                        )
                    )
            if qty_val > 0 and bonus_remaining > 0:
                bonus_qty = min(qty_val, bonus_remaining)
                if bonus_qty > 0:
                    bonus_allocations_all.append((line["item_id"], lot, bonus_qty))
                    bonus_remaining -= bonus_qty

        if to_location and to_location.type == "pharmacy" and line["doctor_id"]:
            rule = find_commission_rule(
                db, line["doctor_id"], int(to_location_id), line["item_id"], sale_date.date()
            )
            if rule:
                commission = Decimal("0")
                for lot, qty_val in sale_allocations:
                    if rule.commission_type == "percent":
                        commission += qty_val * line["unit_price"] * (rule.commission_value / Decimal("100"))
                    else:
                        commission += qty_val * rule.commission_value
                sale_line.commission_amount = commission
                db.add(
                    DoctorTransaction(
                        date=sale_date,
                        doctor_id=line["doctor_id"],
                        pharmacy_location_id=int(to_location_id),
                        transfer_id=sale.id,
                        type="commission_earned",
                        amount=commission,
                        notes="عمولة طبيب",
                    )
                )

    if errors:
        db.rollback()
        return await create_sale(
            request,
            date=date,
            to_location_id=to_location_id,
            rep_id=rep_id,
            price_category=price_category,
            notes=notes,
            item_id=item_id,
            doctor_id=doctor_id,
            requested_qty=requested_qty,
            unit_price=unit_price,
            bonus_amount=bonus_amount,
            discount_amount=discount_amount,
            db=db,
        )

    if bonus_allocations_all:
        damage = DamageNote(
            date=sale_date,
            location_id=main_wh.id,
            notes=f"بونص مبيعات - فاتورة #{sale.id}",
        )
        db.add(damage)
        db.flush()
        for item_id_val, lot, bonus_qty in bonus_allocations_all:
            db.add(
                DamageLine(
                    damage_id=damage.id,
                    item_id=item_id_val,
                    lot_id=lot.id,
                    lot_code_snapshot=lot.lot_code,
                    qty=bonus_qty,
                )
            )
            db.add(
                InventoryMove(
                    date=sale_date,
                    location_id=main_wh.id,
                    item_id=item_id_val,
                    lot_id=lot.id,
                    qty_in=Decimal("0"),
                    qty_out=bonus_qty,
                    source_type="damage",
                    source_id=damage.id,
                    notes="بونص مبيعات",
                )
            )

    sale.total = total
    db.add(
        LocationTransaction(
            date=sale_date,
            location_id=int(to_location_id),
            type="invoice",
            amount=total,
            notes="فاتورة بيع",
            source_type="sale",
            source_id=sale.id,
        )
    )
    db.commit()

    if is_hx:
        sales = db.query(Transfer).filter(Transfer.kind == "sale").order_by(Transfer.id.desc()).all()
        customers = (
            db.query(Location)
            .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        reps = db.query(Representative).order_by(Representative.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
        items_json = json.dumps(items_data, ensure_ascii=False)
        doctors_data = [{"id": d.id, "name": d.name} for d in doctors]
        doctors_json = json.dumps(doctors_data, ensure_ascii=False)
        customer_balance_map = {c.id: get_location_balance(db, c.id) for c in customers}
        return templates.TemplateResponse(
        "sales/sections/sales_list.html",
        {
            "request": request,
            "sales": sales,
            "customers": customers,
            "items": items_data,
            "items_json": items_json,
            "doctors_json": doctors_json,
            "customer_balance_map": customer_balance_map,
            "reps": reps,
            "main_warehouse": main_wh,
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )
    return RedirectResponse(url=f"/sales/{sale.id}", status_code=303)


@app.post("/sales/opening", response_class=HTMLResponse)
async def set_sales_opening_balance(
    request: Request,
    location_id: str = Form(...),
    amount: str = Form("0"),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    clean_id = int(location_id)
    db.query(LocationTransaction).filter(
        LocationTransaction.location_id == clean_id,
        LocationTransaction.type == "opening_balance",
    ).delete()
    if amt != 0:
        db.add(
            LocationTransaction(
                date=datetime.now(),
                location_id=clean_id,
                type="opening_balance",
                amount=amt,
                notes="رصيد أول المدة",
            )
        )
    db.commit()
    customers = (
        db.query(Location)
        .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    opening_rows = (
        db.query(LocationTransaction.location_id, func.coalesce(func.sum(LocationTransaction.amount), 0))
        .filter(LocationTransaction.type == "opening_balance")
        .group_by(LocationTransaction.location_id)
        .all()
    )
    opening_map = {row[0]: Decimal(str(row[1] or 0)) for row in opening_rows}
    return templates.TemplateResponse(
        "sales/opening_table.html",
        {"request": request, "customers": customers, "opening_map": opening_map},
    )


@app.post("/sales/returns/new", response_class=HTMLResponse)
async def create_sale_return(
    request: Request,
    date: str = Form(""),
    from_location_id: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    requested_qty: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    is_hx = request.headers.get("HX-Request") == "true"
    main_wh = get_main_warehouse(db)
    if not from_location_id:
        errors.append("برجاء اختيار العميل.")
    from_location = db.query(Location).filter(Location.id == int(from_location_id or 0)).first()
    if not from_location or from_location.type not in {"pharmacy", "warehouse", "sub_warehouse"}:
        errors.append("الجهة غير صالحة أو ليست صيدلية/مخزن.")
    lines = []
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        qty_val = parse_decimal(requested_qty[idx] if idx < len(requested_qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        if qty_val <= 0:
            errors.append("يرجى إدخال كمية صحيحة.")
            continue
        if price_val < 0:
            errors.append("سعر الصنف لا يمكن أن يكون سالباً.")
            continue
        lines.append(
            {
                "item_id": int(raw_item_id),
                "requested_qty": qty_val,
                "unit_price": price_val,
            }
        )
    if not lines:
        errors.append("يرجى إدخال أصناف للمرتجع.")

    if errors:
        returns = (
            db.query(Transfer)
            .filter(Transfer.kind == "sale_return")
            .order_by(Transfer.id.desc())
            .all()
        )
        customers = (
            db.query(Location)
            .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
        items_json = json.dumps(items_data, ensure_ascii=False)
        return templates.TemplateResponse(
            "sales/sections/returns.html",
            {
                "request": request,
                "returns": returns,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "main_warehouse": main_wh,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    return_date = parse_date(date)
    sale_return = Transfer(
        date=return_date,
        kind="sale_return",
        from_location_id=int(from_location_id),
        to_location_id=main_wh.id,
        notes=clean_text(notes),
        total=Decimal("0"),
    )
    db.add(sale_return)
    db.flush()

    total = Decimal("0")
    for line in lines:
        remaining = line["requested_qty"]
        allocations = []
        available_lots = get_available_lots(db, int(from_location_id), line["item_id"])
        for lot, available in available_lots:
            if remaining <= 0:
                break
            take_qty = min(remaining, available)
            if take_qty <= 0:
                continue
            allocations.append((lot, take_qty))
            remaining -= take_qty
        if remaining > 0:
            item_name = db.query(Item.name).filter(Item.id == line["item_id"]).scalar() or "صنف غير معروف"
            errors.append(f"الكمية المتاحة غير كافية للصنف: {item_name}.")
            break

        line_total = line["requested_qty"] * line["unit_price"]
        total += line_total
        return_line = TransferLine(
            transfer_id=sale_return.id,
            item_id=line["item_id"],
            requested_qty=line["requested_qty"],
            unit_price=line["unit_price"],
            commission_amount=Decimal("0"),
        )
        db.add(return_line)
        db.flush()

        for lot, qty_val in allocations:
            db.add(
                TransferAllocation(
                    transfer_line_id=return_line.id,
                    lot_id=lot.id,
                    qty=qty_val,
                    lot_code_snapshot=lot.lot_code,
                )
            )
            db.add(
                InventoryMove(
                    date=return_date,
                    location_id=int(from_location_id),
                    item_id=line["item_id"],
                    lot_id=lot.id,
                    qty_in=Decimal("0"),
                    qty_out=qty_val,
                    source_type="sale_return",
                    source_id=sale_return.id,
                )
            )
            db.add(
                InventoryMove(
                    date=return_date,
                    location_id=main_wh.id,
                    item_id=line["item_id"],
                    lot_id=lot.id,
                    qty_in=qty_val,
                    qty_out=Decimal("0"),
                    source_type="sale_return",
                    source_id=sale_return.id,
                )
            )

    if errors:
        db.rollback()
        return await create_sale_return(
            request,
            date=date,
            from_location_id=from_location_id,
            notes=notes,
            item_id=item_id,
            requested_qty=requested_qty,
            unit_price=unit_price,
            db=db,
        )

    sale_return.total = total
    db.add(
        LocationTransaction(
            date=return_date,
            location_id=int(from_location_id),
            type="adjustment",
            amount=Decimal("0") - total,
            notes="مرتجع بيع",
            source_type="sale_return",
            source_id=sale_return.id,
        )
    )
    db.commit()

    if is_hx:
        returns = (
            db.query(Transfer)
            .filter(Transfer.kind == "sale_return")
            .order_by(Transfer.id.desc())
            .all()
        )
        customers = (
            db.query(Location)
            .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
        items_json = json.dumps(items_data, ensure_ascii=False)
        return templates.TemplateResponse(
            "sales/sections/returns.html",
            {
                "request": request,
                "returns": returns,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "main_warehouse": main_wh,
                "today": datetime.now().strftime("%Y-%m-%d"),
            },
        )
    return RedirectResponse(url=f"/sales/{sale_return.id}", status_code=303)


@app.get("/sales/{sale_id}", response_class=HTMLResponse)
async def sale_details(request: Request, sale_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    sale = db.query(Transfer).filter(Transfer.id == sale_id).first()
    paid_total = (
        db.query(func.coalesce(func.sum(SalesInvoicePayment.amount), 0))
        .filter(SalesInvoicePayment.sale_id == sale_id)
        .scalar()
        or 0
    )
    paid_total = Decimal(str(paid_total))
    prev_balance = get_location_balance(
        db,
        sale.to_location_id if sale else None,
        up_to=sale.date if sale else None,
        exclude_sale_id=sale.id if sale else None,
    )
    invoice_total = Decimal(str(sale.total or 0)) if sale else Decimal("0")
    new_balance = prev_balance + invoice_total
    return templates.TemplateResponse(
        "sales/details.html",
        {
            "request": request,
            "sale": sale,
            "active_page": "sales",
            "prev_balance": prev_balance,
            "invoice_total": invoice_total,
            "new_balance": new_balance,
            "paid_total": paid_total,
            "remaining_total": invoice_total - paid_total,
        },
    )


@app.get("/sales/{sale_id}/print", response_class=HTMLResponse)
async def sale_print(request: Request, sale_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    sale = db.query(Transfer).filter(Transfer.id == sale_id).first()
    settings = get_print_settings()
    paid_total = (
        db.query(func.coalesce(func.sum(SalesInvoicePayment.amount), 0))
        .filter(SalesInvoicePayment.sale_id == sale_id)
        .scalar()
        or 0
    )
    paid_total = Decimal(str(paid_total))
    prev_balance = get_location_balance(
        db,
        sale.to_location_id if sale else None,
        up_to=sale.date if sale else None,
        exclude_sale_id=sale.id if sale else None,
    )
    invoice_total = Decimal(str(sale.total or 0)) if sale else Decimal("0")
    new_balance = prev_balance + invoice_total
    return templates.TemplateResponse(
        "sales/print.html",
        {
            "request": request,
            "sale": sale,
            "settings": settings,
            "prev_balance": prev_balance,
            "invoice_total": invoice_total,
            "new_balance": new_balance,
            "paid_total": paid_total,
            "remaining_total": invoice_total - paid_total,
        },
    )


@app.delete("/sales/{sale_id}", response_class=HTMLResponse)
async def delete_sale(request: Request, sale_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    sale = db.query(Transfer).filter(Transfer.id == sale_id).first()
    if sale:
        has_payments = (
            db.query(SalesInvoicePayment.id)
            .filter(SalesInvoicePayment.sale_id == sale_id)
            .first()
            is not None
        )
        if has_payments:
            return HTMLResponse("لا يمكن حذف الفاتورة لوجود مدفوعات مرتبطة بها.", status_code=400)
    if sale:
        db.query(DoctorTransaction).filter(DoctorTransaction.transfer_id == sale_id).delete()
        db.query(LocationTransaction).filter(
            LocationTransaction.source_type.in_(["sale", "sale_return"]),
            LocationTransaction.source_id == sale_id,
        ).delete()
        db.query(InventoryMove).filter(
            InventoryMove.source_type.in_(["sale", "sale_return"]),
            InventoryMove.source_id == sale_id,
        ).delete()
        db.delete(sale)
        db.commit()
    sales = db.query(Transfer).filter(Transfer.kind == "sale").order_by(Transfer.id.desc()).all()
    return templates.TemplateResponse("sales/table.html", {"request": request, "sales": sales})


@app.post("/sales/orders", response_class=HTMLResponse)
async def create_sales_order(
    request: Request,
    date: str = Form(""),
    location_id: str = Form(""),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    qty: List[str] = Form([]),
    unit_price: List[str] = Form([]),
    db: Session = Depends(get_db),
user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    clean_location_id = int(location_id) if location_id else None
    if not clean_location_id:
        orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
        pharmacies = (
            db.query(Location)
            .filter(Location.type == "pharmacy")
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
        items_json = json.dumps(items_data, ensure_ascii=False)
        return templates.TemplateResponse(
            "sales/sections/orders.html",
            {
                "request": request,
                "orders": orders,
                "pharmacies": pharmacies,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": ["برجاء اختيار العميل."],
            },
        )

    order = SalesOrder(
        date=parse_date(date),
        location_id=clean_location_id,
        notes=clean_text(notes),
        status="open",
        total=Decimal("0"),
    )
    db.add(order)
    db.flush()

    total = Decimal("0")
    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        qty_val = parse_decimal(qty[idx] if idx < len(qty) else "0")
        price_val = parse_decimal(unit_price[idx] if idx < len(unit_price) else "0")
        if qty_val <= 0:
            continue
        line_total = qty_val * price_val
        total += line_total
        db.add(
            SalesOrderLine(
                order_id=order.id,
                item_id=int(raw_item_id),
                qty=qty_val,
                unit_price=price_val,
                total=line_total,
            )
        )

    order.total = total
    db.commit()

    orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
    return templates.TemplateResponse(
        "sales/orders_table.html",
        {"request": request, "orders": orders},
    )


@app.delete("/sales/orders/{order_id}", response_class=HTMLResponse)
async def delete_sales_order(request: Request, order_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    order = db.query(SalesOrder).filter(SalesOrder.id == order_id).first()
    if order:
        db.delete(order)
        db.commit()
    orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
    return templates.TemplateResponse(
        "sales/orders_table.html",
        {"request": request, "orders": orders},
    )


@app.post("/sales/orders/{order_id}/convert", response_class=HTMLResponse)
async def convert_sales_order(request: Request, order_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    order = db.query(SalesOrder).filter(SalesOrder.id == order_id).first()
    if not order:
        errors.append("الطلب غير موجود.")
    elif order.status == "converted":
        errors.append("الطلب تم تحويله مسبقاً.")

    main_wh = get_main_warehouse(db)
    location = order.location if order else None
    if location and location.type not in {"pharmacy", "warehouse", "sub_warehouse"}:
        errors.append("الجهة غير صالحة أو ليست صيدلية/مخزن.")

    if not order or errors:
        orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
        customers = (
            db.query(Location)
            .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
        items_json = json.dumps(items_data, ensure_ascii=False)
        return templates.TemplateResponse(
            "sales/sections/orders.html",
            {
                "request": request,
                "orders": orders,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors or ["حدث خطأ غير متوقع."],
            },
        )

    sale_date = order.date or datetime.now()
    sale = Transfer(
        date=sale_date,
        kind="sale",
        price_category=None,
        from_location_id=main_wh.id,
        to_location_id=order.location_id,
        rep_id=None,
        notes=f"تحويل من أمر بيع #{order.id}",
        total=Decimal("0"),
    )
    db.add(sale)
    db.flush()

    total = Decimal("0")
    for line in order.lines:
        remaining = Decimal(str(line.qty or 0))
        allocations = []
        available_lots = get_available_lots(db, main_wh.id, line.item_id)
        for lot, available in available_lots:
            if remaining <= 0:
                break
            take_qty = min(remaining, available)
            if take_qty <= 0:
                continue
            allocations.append((lot, take_qty))
            remaining -= take_qty
        if remaining > 0:
            item_name = db.query(Item.name).filter(Item.id == line.item_id).scalar() or "صنف غير معروف"
            errors.append(f"الكمية المتاحة غير كافية للصنف: {item_name}.")
            break

        line_total = Decimal(str(line.qty or 0)) * Decimal(str(line.unit_price or 0))
        total += line_total
        sale_line = TransferLine(
            transfer_id=sale.id,
            item_id=line.item_id,
            requested_qty=line.qty,
            unit_price=line.unit_price,
            commission_amount=Decimal("0"),
            bonus_amount=Decimal("0"),
            line_total=line_total,
        )
        db.add(sale_line)
        db.flush()

        for lot, qty_val in allocations:
            db.add(
                TransferAllocation(
                    transfer_line_id=sale_line.id,
                    lot_id=lot.id,
                    qty=qty_val,
                    lot_code_snapshot=lot.lot_code,
                )
            )
            db.add(
                InventoryMove(
                    date=sale_date,
                    location_id=main_wh.id,
                    item_id=line.item_id,
                    lot_id=lot.id,
                    qty_in=Decimal("0"),
                    qty_out=qty_val,
                    source_type="sale",
                    source_id=sale.id,
                )
            )
            db.add(
                InventoryMove(
                    date=sale_date,
                    location_id=order.location_id,
                    item_id=line.item_id,
                    lot_id=lot.id,
                    qty_in=qty_val,
                    qty_out=Decimal("0"),
                    source_type="sale",
                    source_id=sale.id,
                )
            )

    if errors:
        db.rollback()
        orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
        customers = (
            db.query(Location)
            .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        items = db.query(Item).order_by(Item.name.asc()).all()
        items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
        items_json = json.dumps(items_data, ensure_ascii=False)
        return templates.TemplateResponse(
            "sales/sections/orders.html",
            {
                "request": request,
                "orders": orders,
                "customers": customers,
                "items": items_data,
                "items_json": items_json,
                "today": datetime.now().strftime("%Y-%m-%d"),
                "errors": errors,
            },
        )

    sale.total = total
    if location and location.type == "pharmacy":
        db.add(
            LocationTransaction(
                date=sale_date,
                location_id=order.location_id,
                type="invoice",
                amount=total,
                notes="فاتورة بيع",
                source_type="sale",
                source_id=sale.id,
            )
        )
    order.status = "converted"
    db.commit()

    orders = db.query(SalesOrder).order_by(SalesOrder.id.desc()).all()
    customers = (
        db.query(Location)
        .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    items = db.query(Item).order_by(Item.name.asc()).all()
    items_data = [{"id": i.id, "name": i.name, "sale_price": float(i.sale_price or 0)} for i in items]
    items_json = json.dumps(items_data, ensure_ascii=False)
    return templates.TemplateResponse(
        "sales/sections/orders.html",
        {
            "request": request,
            "orders": orders,
            "customers": customers,
            "items": items_data,
            "items_json": items_json,
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )


def build_customer_unpaid_sales_rows(db: Session, customer_id: Optional[int]):
    if not customer_id:
        return []
    sales = (
        db.query(Transfer)
        .filter(Transfer.kind == "sale", Transfer.to_location_id == customer_id)
        .order_by(Transfer.id.desc())
        .all()
    )
    payments = (
        db.query(SalesInvoicePayment.sale_id, func.coalesce(func.sum(SalesInvoicePayment.amount), 0))
        .group_by(SalesInvoicePayment.sale_id)
        .all()
    )
    paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in payments}
    rows = []
    for sale in sales:
        total = Decimal(str(sale.total or 0))
        paid = paid_map.get(sale.id, Decimal("0"))
        remaining = total - paid
        if remaining <= 0:
            continue
        rows.append(
            {
                "sale": sale,
                "total": total,
                "paid": paid,
                "remaining": remaining,
            }
        )
    return rows


# -------------------------
# Treasury (Cash)
# -------------------------
@app.get("/treasury", response_class=HTMLResponse)
async def treasury_page(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    accounts = db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all()
    balances = {acc.id: get_cash_balance(db, acc.id) for acc in accounts}
    transfers = (
        db.query(CashTransaction)
        .filter(CashTransaction.type == "transfer")
        .order_by(CashTransaction.id.desc())
        .limit(30)
        .all()
    )
    return templates.TemplateResponse(
        "treasury/page.html",
        {
            "request": request,
            "accounts": accounts,
            "balances": balances,
            "transfers": transfers,
            "active_page": "treasury",
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )


@app.get("/treasury/reports", response_class=HTMLResponse)
async def treasury_reports_page(request: Request,user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "treasury/reports.html",
        {
            "request": request,
            "active_page": "treasury",
        },
    )


@app.get("/treasury/section/{section}", response_class=HTMLResponse)
async def treasury_section(request: Request, section: str, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    accounts = db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all()
    balances = {acc.id: get_cash_balance(db, acc.id) for acc in accounts}
    today = datetime.now().strftime("%Y-%m-%d")

    if section == "reports":
        return templates.TemplateResponse(
            "treasury/sections/reports.html",
            {
                "request": request,
                "accounts": accounts,
                "balances": balances,
                "today": today,
            },
        )

    if section == "opening":
        opening_map = {acc.id: get_cash_opening_balance(db, acc.id) for acc in accounts}
        return templates.TemplateResponse(
            "treasury/sections/opening.html",
            {
                "request": request,
                "accounts": accounts,
                "opening_map": opening_map,
                "today": today,
            },
        )

    if section == "collection":
        customers = (
            db.query(Location)
            .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
            .order_by(Location.name.asc())
            .all()
        )
        customer_id_raw = str(request.query_params.get("customer_id") or "").strip()
        customer_id = int(customer_id_raw) if customer_id_raw.isdigit() else 0
        selected_customer = db.query(Location).filter(Location.id == customer_id).first() if customer_id else None
        default_account = (
            get_customer_default_cash_account(db, selected_customer)
            if selected_customer
            else get_main_cash_account(db)
        )
        account_id_raw = str(request.query_params.get("cash_account_id") or "").strip()
        account_id = int(account_id_raw) if account_id_raw.isdigit() else 0
        selected_account = (
            db.query(CashAccount).filter(CashAccount.id == account_id).first()
            if account_id
            else default_account
        )
        selected_date = request.query_params.get("date") or today
        rows = build_customer_unpaid_sales_rows(db, customer_id if selected_customer else None)
        return templates.TemplateResponse(
            "treasury/sections/collection.html",
            {
                "request": request,
                "customers": customers,
                "selected_customer": selected_customer,
                "accounts": accounts,
                "selected_account": selected_account,
                "rows": rows,
                "today": selected_date,
                "selected_date": selected_date,
            },
        )

    if section == "payment":
        suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
        supplier_id = int(request.query_params.get("supplier_id") or 0)
        selected_supplier = db.query(Supplier).filter(Supplier.id == supplier_id).first() if supplier_id else None
        account_id = int(request.query_params.get("cash_account_id") or 0)
        selected_account = (
            db.query(CashAccount).filter(CashAccount.id == account_id).first()
            if account_id
            else get_main_cash_account(db)
        )
        purchases = (
            db.query(Purchase)
            .filter(Purchase.kind == "purchase")
            .order_by(Purchase.id.desc())
            .all()
        )
        payments = (
            db.query(PurchaseInvoicePayment.purchase_id, func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
            .group_by(PurchaseInvoicePayment.purchase_id)
            .all()
        )
        paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in payments}
        rows = []
        for purchase in purchases:
            if supplier_id and purchase.supplier_id != supplier_id:
                continue
            total = Decimal(str(purchase.total or 0))
            paid = paid_map.get(purchase.id, Decimal("0"))
            remaining = total - paid
            if remaining <= 0:
                continue
            rows.append(
                {
                    "purchase": purchase,
                    "total": total,
                    "paid": paid,
                    "remaining": remaining,
                }
            )
        return templates.TemplateResponse(
            "treasury/sections/payment.html",
            {
                "request": request,
                "suppliers": suppliers,
                "selected_supplier": selected_supplier,
                "accounts": accounts,
                "selected_account": selected_account,
                "rows": rows,
                "today": today,
            },
        )
    if section == "doctor_payment":
        doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
        balances = (
            db.query(DoctorTransaction.doctor_id, func.coalesce(func.sum(DoctorTransaction.amount), 0))
            .group_by(DoctorTransaction.doctor_id)
            .all()
        )
        balance_map = {row[0]: Decimal(str(row[1] or 0)) for row in balances}
        rows = []
        for doc in doctors:
            bal = balance_map.get(doc.id, Decimal("0"))
            if bal <= 0:
                continue
            rows.append({"doctor": doc, "balance": bal})
        account_id = int(request.query_params.get("cash_account_id") or 0)
        selected_account = (
            db.query(CashAccount).filter(CashAccount.id == account_id).first()
            if account_id
            else get_main_cash_account(db)
        )
        return templates.TemplateResponse(
            "treasury/sections/doctor_payment.html",
            {
                "request": request,
                "rows": rows,
                "accounts": accounts,
                "selected_account": selected_account,
                "today": today,
            },
        )

    transfers = (
        db.query(CashTransaction)
        .filter(CashTransaction.type == "transfer")
        .order_by(CashTransaction.id.desc())
        .limit(30)
        .all()
    )
    return templates.TemplateResponse(
        "treasury/sections/transfer.html",
        {
            "request": request,
            "accounts": accounts,
            "balances": balances,
            "transfers": transfers,
            "today": today,
        },
    )


@app.post("/treasury/opening", response_class=HTMLResponse)
async def treasury_opening_balance(
    request: Request,
    account_id: str = Form(""),
    amount: str = Form(""),
    date: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    account_id_val = int(account_id) if account_id else 0
    if not account_id_val:
        accounts = db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all()
        opening_map = {acc.id: get_cash_opening_balance(db, acc.id) for acc in accounts}
        return templates.TemplateResponse(
            "treasury/opening_table.html",
            {"request": request, "accounts": accounts, "opening_map": opening_map},
        )

    amt = parse_decimal(amount or "0")
    date_val = parse_date(date) if date else datetime.now()

    existing = (
        db.query(CashTransaction)
        .filter(CashTransaction.type == "opening_balance")
        .filter(
            (CashTransaction.to_account_id == account_id_val)
            | (CashTransaction.from_account_id == account_id_val)
        )
        .all()
    )
    for row in existing:
        db.delete(row)

    if amt != 0:
        if amt > 0:
            db.add(
                CashTransaction(
                    date=date_val,
                    type="opening_balance",
                    amount=amt,
                    to_account_id=account_id_val,
                    notes="رصيد أول المدة",
                )
            )
        else:
            db.add(
                CashTransaction(
                    date=date_val,
                    type="opening_balance",
                    amount=abs(amt),
                    from_account_id=account_id_val,
                    notes="رصيد أول المدة",
                )
            )
    db.commit()

    accounts = db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all()
    opening_map = {acc.id: get_cash_opening_balance(db, acc.id) for acc in accounts}
    return templates.TemplateResponse(
        "treasury/opening_table.html",
        {"request": request, "accounts": accounts, "opening_map": opening_map},
    )


@app.post("/treasury/transfer", response_class=HTMLResponse)
async def treasury_transfer(
    request: Request,
    from_account_id: str = Form(""),
    to_account_id: str = Form(""),
    date: str = Form(""),
    amount: str = Form("0"),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    amt = parse_decimal(amount)
    from_id = int(from_account_id) if from_account_id else None
    to_id = int(to_account_id) if to_account_id else None
    if from_id and to_id and amt > 0 and from_id != to_id:
        db.add(
            CashTransaction(
                date=parse_date(date),
                type="transfer",
                amount=amt,
                from_account_id=from_id,
                to_account_id=to_id,
                notes=clean_text(notes),
            )
        )
        db.commit()

    accounts = db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all()
    balances = {acc.id: get_cash_balance(db, acc.id) for acc in accounts}
    transfers = (
        db.query(CashTransaction)
        .filter(CashTransaction.type == "transfer")
        .order_by(CashTransaction.id.desc())
        .limit(30)
        .all()
    )
    return templates.TemplateResponse(
        "treasury/sections/transfer.html",
        {
            "request": request,
            "accounts": accounts,
            "balances": balances,
            "transfers": transfers,
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )


@app.post("/treasury/collections", response_class=HTMLResponse)
async def treasury_collections(
    request: Request,
    customer_id: str = Form(""),
    cash_account_id: str = Form(""),
    date: str = Form(""),
    sale_id: List[str] = Form([]),
    pay_amount: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    customer_id_raw = str(customer_id or "").strip()
    customer_id_val = int(customer_id_raw) if customer_id_raw.isdigit() else 0
    if not customer_id_val:
        errors.append("برجاء اختيار العميل.")
    selected_customer = (
        db.query(Location).filter(Location.id == customer_id_val).first() if customer_id_val else None
    )
    if customer_id_val and not selected_customer:
        errors.append("العميل غير موجود.")

    account_id_raw = str(cash_account_id or "").strip()
    account_id_val = int(account_id_raw) if account_id_raw.isdigit() else 0
    default_account = (
        get_customer_default_cash_account(db, selected_customer)
        if selected_customer
        else get_main_cash_account(db)
    )
    selected_account = (
        db.query(CashAccount).filter(CashAccount.id == account_id_val).first()
        if account_id_val
        else default_account
    )
    if not selected_account:
        errors.append("برجاء اختيار الخزنة.")

    payments_sum = (
        db.query(SalesInvoicePayment.sale_id, func.coalesce(func.sum(SalesInvoicePayment.amount), 0))
        .group_by(SalesInvoicePayment.sale_id)
        .all()
    )
    paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in payments_sum}
    customer_rep_id = get_location_default_rep_id(selected_customer) if selected_customer else None
    pay_date = parse_date(date) if clean_text(date) else datetime.now()

    if selected_account and selected_customer and not errors:
        for idx, raw_sale_id in enumerate(sale_id):
            if not raw_sale_id:
                continue
            sale_id_raw = str(raw_sale_id).strip()
            if not sale_id_raw.isdigit():
                continue
            amount_val = parse_decimal(pay_amount[idx] if idx < len(pay_amount) else "0")
            if amount_val <= 0:
                continue
            sale = db.query(Transfer).filter(Transfer.id == int(sale_id_raw)).first()
            if not sale:
                continue
            if sale.to_location_id != selected_customer.id:
                errors.append(f"الفاتورة رقم {sale.id} ليست للعميل المختار.")
                continue
            total = Decimal(str(sale.total or 0))
            paid = paid_map.get(sale.id, Decimal("0"))
            remaining = total - paid
            if amount_val > remaining:
                errors.append(f"المبلغ أكبر من المتبقي للفاتورة رقم {sale.id}.")
                continue
            payment_rep_id = sale.rep_id or customer_rep_id

            db.add(
                SalesInvoicePayment(
                    sale_id=sale.id,
                    rep_id=payment_rep_id,
                    cash_account_id=selected_account.id,
                    date=pay_date,
                    amount=amount_val,
                    notes="تحصيل فاتورة بيع",
                )
            )
            db.add(
                LocationTransaction(
                    date=pay_date,
                    location_id=sale.to_location_id,
                    type="payment",
                    amount=-amount_val,
                    notes="تحصيل فاتورة بيع",
                    source_type="sale_payment",
                    source_id=sale.id,
                )
            )
            db.add(
                CashTransaction(
                    date=pay_date,
                    type="collection",
                    amount=amount_val,
                    from_account_id=None,
                    to_account_id=selected_account.id,
                    notes="تحصيل فاتورة بيع",
                    source_type="sale_payment",
                    source_id=sale.id,
                )
            )

        if not errors:
            db.commit()

    customers = (
        db.query(Location)
        .filter(Location.type.in_(["pharmacy", "warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    accounts = db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all()
    selected_customer = (
        db.query(Location).filter(Location.id == customer_id_val).first() if customer_id_val else None
    )
    default_account = (
        get_customer_default_cash_account(db, selected_customer)
        if selected_customer
        else get_main_cash_account(db)
    )
    selected_account = (
        db.query(CashAccount).filter(CashAccount.id == account_id_val).first()
        if account_id_val
        else default_account
    )
    rows = build_customer_unpaid_sales_rows(db, customer_id_val if selected_customer else None)
    selected_date = date if clean_text(date) else datetime.now().strftime("%Y-%m-%d")
    return templates.TemplateResponse(
        "treasury/sections/collection.html",
        {
            "request": request,
            "customers": customers,
            "selected_customer": selected_customer,
            "accounts": accounts,
            "selected_account": selected_account,
            "rows": rows,
            "today": selected_date,
            "selected_date": selected_date,
            "errors": errors,
        },
    )


@app.post("/treasury/payments", response_class=HTMLResponse)
async def treasury_payments(
    request: Request,
    supplier_id: str = Form(""),
    cash_account_id: str = Form(""),
    date: str = Form(""),
    purchase_id: List[str] = Form([]),
    pay_amount: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    if not supplier_id:
        errors.append("برجاء اختيار المورد.")
    supplier_id_val = int(supplier_id) if supplier_id else 0
    account_id_val = int(cash_account_id) if cash_account_id else 0
    selected_account = (
        db.query(CashAccount).filter(CashAccount.id == account_id_val).first()
        if account_id_val
        else get_main_cash_account(db)
    )
    if not selected_account:
        errors.append("برجاء اختيار الخزنة.")

    payments_sum = (
        db.query(PurchaseInvoicePayment.purchase_id, func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
        .group_by(PurchaseInvoicePayment.purchase_id)
        .all()
    )
    paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in payments_sum}

    if supplier_id_val and selected_account and not errors:
        for idx, raw_purchase_id in enumerate(purchase_id):
            if not raw_purchase_id:
                continue
            amount_val = parse_decimal(pay_amount[idx] if idx < len(pay_amount) else "0")
            if amount_val <= 0:
                continue
            purchase = db.query(Purchase).filter(Purchase.id == int(raw_purchase_id)).first()
            if not purchase:
                continue
            total = Decimal(str(purchase.total or 0))
            paid = paid_map.get(purchase.id, Decimal("0"))
            remaining = total - paid
            if amount_val > remaining:
                errors.append(f"المبلغ أكبر من المتبقي لفاتورة الشراء رقم {purchase.id}.")
                continue
            payment = PurchaseInvoicePayment(
                purchase_id=purchase.id,
                cash_account_id=selected_account.id,
                date=parse_date(date),
                amount=amount_val,
                notes="سداد فاتورة شراء",
            )
            db.add(payment)
            db.flush()
            add_supplier_transaction(
                db,
                supplier_id=purchase.supplier_id,
                date=payment.date,
                amount=Decimal("0") - amount_val,
                tx_type="payment",
                notes=payment.notes,
                source_type="purchase_payment",
                source_id=payment.id,
            )
            db.flush()
            db.add(
                CashTransaction(
                    date=parse_date(date),
                    type="supplier_payment",
                    amount=amount_val,
                    from_account_id=selected_account.id,
                    to_account_id=None,
                    notes="سداد فاتورة شراء",
                    source_type="purchase_payment",
                    source_id=purchase.id,
                )
            )

        if not errors:
            if supplier_id_val:
                rebuild_supplier_ledger_for_supplier(db, supplier_id_val)
            db.commit()

    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    selected_supplier = db.query(Supplier).filter(Supplier.id == supplier_id_val).first() if supplier_id_val else None
    accounts = db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all()
    selected_account = (
        db.query(CashAccount).filter(CashAccount.id == account_id_val).first()
        if account_id_val
        else get_main_cash_account(db)
    )
    purchases = (
        db.query(Purchase)
        .filter(Purchase.kind == "purchase")
        .order_by(Purchase.id.desc())
        .all()
    )
    payments = (
        db.query(PurchaseInvoicePayment.purchase_id, func.coalesce(func.sum(PurchaseInvoicePayment.amount), 0))
        .group_by(PurchaseInvoicePayment.purchase_id)
        .all()
    )
    paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in payments}
    rows = []
    for purchase in purchases:
        if supplier_id_val and purchase.supplier_id != supplier_id_val:
            continue
        total = Decimal(str(purchase.total or 0))
        paid = paid_map.get(purchase.id, Decimal("0"))
        remaining = total - paid
        if remaining <= 0:
            continue
        rows.append(
            {
                "purchase": purchase,
                "total": total,
                "paid": paid,
                "remaining": remaining,
            }
        )
    return templates.TemplateResponse(
        "treasury/sections/payment.html",
        {
            "request": request,
            "suppliers": suppliers,
            "selected_supplier": selected_supplier,
            "accounts": accounts,
            "selected_account": selected_account,
            "rows": rows,
            "today": datetime.now().strftime("%Y-%m-%d"),
            "errors": errors,
        },
    )


@app.post("/treasury/doctor_payments", response_class=HTMLResponse)
async def treasury_doctor_payments(
    request: Request,
    date: str = Form(""),
    cash_account_id: str = Form(""),
    doctor_id: List[str] = Form([]),
    pay_amount: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    errors = []
    account_id_val = int(cash_account_id) if cash_account_id else 0
    selected_account = (
        db.query(CashAccount).filter(CashAccount.id == account_id_val).first()
        if account_id_val
        else get_main_cash_account(db)
    )
    if not selected_account:
        errors.append("برجاء اختيار الخزنة.")
    balances = (
        db.query(DoctorTransaction.doctor_id, func.coalesce(func.sum(DoctorTransaction.amount), 0))
        .group_by(DoctorTransaction.doctor_id)
        .all()
    )
    balance_map = {row[0]: Decimal(str(row[1] or 0)) for row in balances}
    for idx, raw_doctor_id in enumerate(doctor_id):
        if not raw_doctor_id:
            continue
        amount_val = parse_decimal(pay_amount[idx] if idx < len(pay_amount) else "0")
        if amount_val <= 0:
            continue
        doc_id = int(raw_doctor_id)
        remaining = balance_map.get(doc_id, Decimal("0"))
        if amount_val > remaining:
            errors.append(f"المبلغ أكبر من المتبقي للطبيب رقم {doc_id}.")
            continue
        db.add(
            DoctorTransaction(
                date=parse_date(date),
                doctor_id=doc_id,
                type="payment",
                amount=Decimal("0") - amount_val,
                notes="سداد عمولة طبيب",
            )
        )
        db.add(
            CashTransaction(
                date=parse_date(date),
                type="doctor_payment",
                amount=amount_val,
                from_account_id=selected_account.id,
                to_account_id=None,
                notes="صرف عمولة طبيب",
                source_type="doctor_payment",
                source_id=doc_id,
            )
        )
    if not errors:
        db.commit()

    doctors = db.query(Doctor).order_by(Doctor.name.asc()).all()
    balances = (
        db.query(DoctorTransaction.doctor_id, func.coalesce(func.sum(DoctorTransaction.amount), 0))
        .group_by(DoctorTransaction.doctor_id)
        .all()
    )
    balance_map = {row[0]: Decimal(str(row[1] or 0)) for row in balances}
    rows = []
    for doc in doctors:
        bal = balance_map.get(doc.id, Decimal("0"))
        if bal <= 0:
            continue
        rows.append({"doctor": doc, "balance": bal})
    return templates.TemplateResponse(
        "treasury/sections/doctor_payment.html",
        {
            "request": request,
            "rows": rows,
            "accounts": db.query(CashAccount).order_by(CashAccount.is_main.desc(), CashAccount.name.asc()).all(),
            "selected_account": selected_account,
            "today": datetime.now().strftime("%Y-%m-%d"),
            "errors": errors,
        },
    )

# -------------------------
# Stocktake
# -------------------------
@app.get("/stocktake", response_class=HTMLResponse)
async def stocktake_list(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    stocktakes = db.query(Stocktake).order_by(Stocktake.id.desc()).all()
    return templates.TemplateResponse(
        "stocktake/page.html",
        {"request": request, "stocktakes": stocktakes, "active_page": "stocktake"},
    )


@app.get("/stocktake/new", response_class=HTMLResponse)
async def stocktake_new(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    locations = db.query(Location).order_by(Location.name.asc()).all()
    items = db.query(Item).order_by(Item.name.asc()).all()
    lots = db.query(ItemLot).order_by(ItemLot.lot_code.asc()).all()
    items_json = json.dumps([{"id": i.id, "name": i.name} for i in items], ensure_ascii=False)
    lots_json = json.dumps([{"id": l.id, "lot_code": l.lot_code} for l in lots], ensure_ascii=False)
    return templates.TemplateResponse(
        "stocktake/create.html",
        {
            "request": request,
            "locations": locations,
            "items_json": items_json,
            "lots_json": lots_json,
            "today": datetime.now().strftime("%Y-%m-%d"),
        },
    )


@app.post("/stocktake/new", response_class=HTMLResponse)
async def stocktake_create(
    request: Request,
    date: str = Form(""),
    location_id: str = Form(...),
    notes: str = Form(""),
    item_id: List[str] = Form([]),
    lot_id: List[str] = Form([]),
    counted_qty: List[str] = Form([]),
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    stocktake = Stocktake(
        date=parse_date(date),
        location_id=int(location_id),
        notes=clean_text(notes),
        status="draft",
    )
    db.add(stocktake)
    db.flush()

    for idx, raw_item_id in enumerate(item_id):
        if not raw_item_id:
            continue
        lot_ref = lot_id[idx] if idx < len(lot_id) else ""
        counted = parse_decimal(counted_qty[idx] if idx < len(counted_qty) else "0")
        query = db.query(func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0)).filter(
            InventoryMove.location_id == int(location_id),
            InventoryMove.item_id == int(raw_item_id),
        )
        if lot_ref:
            query = query.filter(InventoryMove.lot_id == int(lot_ref))
        system_qty = Decimal(str(query.scalar() or 0))
        diff_qty = counted - system_qty
        db.add(
            StocktakeLine(
                stocktake_id=stocktake.id,
                item_id=int(raw_item_id),
                lot_id=int(lot_ref) if lot_ref else None,
                counted_qty=counted,
                system_qty=system_qty,
                diff_qty=diff_qty,
            )
        )
    db.commit()
    return RedirectResponse(url=f"/stocktake/{stocktake.id}", status_code=303)


@app.get("/stocktake/{stocktake_id}", response_class=HTMLResponse)
async def stocktake_details(request: Request, stocktake_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    stocktake = db.query(Stocktake).filter(Stocktake.id == stocktake_id).first()
    increase_total = sum((l.diff_qty or 0) for l in stocktake.lines if l.diff_qty > 0) if stocktake else 0
    decrease_total = sum((abs(l.diff_qty or 0)) for l in stocktake.lines if l.diff_qty < 0) if stocktake else 0
    return templates.TemplateResponse(
        "stocktake/details.html",
        {
            "request": request,
            "stocktake": stocktake,
            "increase_total": increase_total,
            "decrease_total": decrease_total,
            "active_page": "stocktake",
        },
    )


@app.post("/stocktake/{stocktake_id}/post", response_class=HTMLResponse)
async def stocktake_post(request: Request, stocktake_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    stocktake = db.query(Stocktake).filter(Stocktake.id == stocktake_id).first()
    if stocktake and stocktake.status != "posted":
        for line in stocktake.lines:
            if line.diff_qty > 0:
                db.add(
                    InventoryMove(
                        date=stocktake.date,
                        location_id=stocktake.location_id,
                        item_id=line.item_id,
                        lot_id=line.lot_id,
                        qty_in=line.diff_qty,
                        qty_out=Decimal("0"),
                        source_type="adjustment",
                        source_id=stocktake.id,
                        notes="stocktake",
                    )
                )
            elif line.diff_qty < 0:
                db.add(
                    InventoryMove(
                        date=stocktake.date,
                        location_id=stocktake.location_id,
                        item_id=line.item_id,
                        lot_id=line.lot_id,
                        qty_in=Decimal("0"),
                        qty_out=abs(line.diff_qty),
                        source_type="adjustment",
                        source_id=stocktake.id,
                        notes="stocktake",
                    )
                )
        stocktake.status = "posted"
        db.commit()
    return RedirectResponse(url=f"/stocktake/{stocktake_id}", status_code=303)


@app.get("/stocktake/{stocktake_id}/print", response_class=HTMLResponse)
async def stocktake_print(request: Request, stocktake_id: int, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    stocktake = db.query(Stocktake).filter(Stocktake.id == stocktake_id).first()
    increase_total = sum((l.diff_qty or 0) for l in stocktake.lines if l.diff_qty > 0) if stocktake else 0
    decrease_total = sum((abs(l.diff_qty or 0)) for l in stocktake.lines if l.diff_qty < 0) if stocktake else 0
    settings = get_print_settings()
    return templates.TemplateResponse(
        "stocktake/print.html",
        {
            "request": request,
            "stocktake": stocktake,
            "increase_total": increase_total,
            "decrease_total": decrease_total,
            "print_date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "settings": settings,
        },
    )


# -------------------------
# Reports
# -------------------------
@app.get("/reports", response_class=HTMLResponse)
async def reports_index(request: Request,user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        "reports/index.html", {"request": request, "active_page": "reports"}
    )


@app.get("/reports/rep-performance", response_class=HTMLResponse)
async def report_rep_performance(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    query = db.query(
        Transfer.rep_id,
        func.coalesce(func.sum(Transfer.total), 0).label("total"),
        func.count(Transfer.id).label("count"),
    ).filter(Transfer.kind == "sale")
    if start_date:
        query = query.filter(Transfer.date >= parse_date(start_date))
    if end_date:
        query = query.filter(Transfer.date <= parse_date_end(end_date))
    rows = query.group_by(Transfer.rep_id).order_by(func.sum(Transfer.total).desc()).all()
    reps = {r.id: r for r in db.query(Representative).all()}
    return templates.TemplateResponse(
        "reports/rep_performance.html",
        {
            "request": request,
            "rows": rows,
            "reps": reps,
            "start_date": start_date,
            "end_date": end_date,
            "active_page": "reports",
        },
    )


@app.get("/reports/region-sales", response_class=HTMLResponse)
async def report_region_sales(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    region: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    base = (
        db.query(Transfer, Location)
        .join(Location, Transfer.to_location_id == Location.id)
        .filter(Transfer.kind == "sale")
    )
    if start_date:
        base = base.filter(Transfer.date >= parse_date(start_date))
    if end_date:
        base = base.filter(Transfer.date <= parse_date_end(end_date))

    region_rows = (
        db.query(
            Location.region,
            func.coalesce(func.sum(Transfer.total), 0).label("total"),
            func.count(Transfer.id).label("count"),
        )
        .join(Location, Transfer.to_location_id == Location.id)
        .filter(
            Transfer.kind == "sale",
            Location.region.isnot(None),
            Location.region != "",
        )
    )
    if start_date:
        region_rows = region_rows.filter(Transfer.date >= parse_date(start_date))
    if end_date:
        region_rows = region_rows.filter(Transfer.date <= parse_date_end(end_date))
    region_rows = region_rows.group_by(Location.region).order_by(func.sum(Transfer.total).desc()).all()

    location_rows = []
    if region:
        location_rows = (
            db.query(
                Location.id,
                Location.name,
                Location.type,
                func.coalesce(func.sum(Transfer.total), 0).label("total"),
                func.count(Transfer.id).label("count"),
            )
            .join(Location, Transfer.to_location_id == Location.id)
            .filter(Transfer.kind == "sale", Location.region == region)
            .group_by(Location.id, Location.name)
            .order_by(func.sum(Transfer.total).desc())
            .all()
        )

    regions = (
        db.query(Location.region)
        .filter(Location.region.isnot(None), Location.region != "")
        .distinct()
        .order_by(Location.region.asc())
        .all()
    )
    return templates.TemplateResponse(
        "reports/region_sales.html",
        {
            "request": request,
            "region_rows": region_rows,
            "location_rows": location_rows,
            "regions": [r[0] for r in regions],
            "selected_region": region,
            "start_date": start_date,
            "end_date": end_date,
            "active_page": "reports",
        },
    )


@app.get("/reports/stock", response_class=HTMLResponse)
async def report_stock(request: Request, location_id: Optional[str] = None, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    locations = db.query(Location).order_by(Location.name.asc()).all()
    main_wh = get_main_warehouse(db)
    selected_id = int(location_id) if location_id else main_wh.id
    rows = (
        db.query(
            Item.id,
            Item.name,
            Item.purchase_price,
            Item.sale_price,
            func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0).label("balance"),
        )
        .outerjoin(
            InventoryMove,
            (InventoryMove.item_id == Item.id) & (InventoryMove.location_id == selected_id),
        )
        .group_by(Item.id)
        .order_by(Item.id.desc())
        .all()
    )
    return templates.TemplateResponse(
        "reports/stock.html",
        {
            "request": request,
            "rows": rows,
            "locations": locations,
            "selected_location": selected_id,
            "active_page": "reports",
        },
    )


def _decimal_to_float(value: Decimal) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _get_item_movement_totals(base_query):
    totals_row = base_query.with_entities(
        func.coalesce(
            func.sum(
                case(
                    (InventoryMove.source_type.in_(["sale", "sales"]), InventoryMove.qty_out),
                    else_=0,
                )
            ),
            0,
        ).label("sales_total"),
        func.coalesce(
            func.sum(
                case(
                    (InventoryMove.source_type.in_(["purchase", "purchases"]), InventoryMove.qty_in),
                    else_=0,
                )
            ),
            0,
        ).label("purchases_total"),
        func.coalesce(
            func.sum(
                case(
                    (InventoryMove.source_type.in_(["purchase_return", "purchase_returns"]), InventoryMove.qty_out),
                    else_=0,
                )
            ),
            0,
        ).label("purchase_returns_total"),
        func.coalesce(
            func.sum(
                case(
                    (InventoryMove.source_type.in_(["sale_return", "sales_return", "sales_returns"]), InventoryMove.qty_in),
                    else_=0,
                )
            ),
            0,
        ).label("sales_returns_total"),
        func.coalesce(
            func.sum(
                case((InventoryMove.source_type.in_(["damage", "damages"]), InventoryMove.qty_out), else_=0)
            ),
            0,
        ).label("waste_total"),
        func.coalesce(
            func.sum(
                case(
                    (
                        (InventoryMove.source_type == "adjustment")
                        & (InventoryMove.qty_out > 0),
                        InventoryMove.qty_out,
                    ),
                    (InventoryMove.source_type.in_(["shortage"]), InventoryMove.qty_out),
                    else_=0,
                )
            ),
            0,
        ).label("shortage_total"),
        func.coalesce(
            func.sum(
                case((InventoryMove.source_type.in_(["damage", "damages"]), InventoryMove.qty_out), else_=0)
            ),
            0,
        ).label("damage_total"),
        func.coalesce(
            func.sum(
                case(
                    (
                        (InventoryMove.source_type == "adjustment")
                        & (InventoryMove.qty_in > 0),
                        InventoryMove.qty_in,
                    ),
                    (InventoryMove.source_type.in_(["increase"]), InventoryMove.qty_in),
                    else_=0,
                )
            ),
            0,
        ).label("increase_total"),
    ).first()
    return totals_row


def _build_item_movement_report(db: Session, payload: dict):
    item_id = payload.get("item_id")
    if not item_id:
        return None, "يرجى اختيار الصنف أولاً."

    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None, "رقم الصنف غير صالح."

    customer_id = payload.get("customer_id")
    warehouse_id = payload.get("warehouse_id")
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")

    item = db.query(Item).filter(Item.id == item_id).first()
    base_query = db.query(InventoryMove).filter(InventoryMove.item_id == item_id)
    if warehouse_id:
        try:
            warehouse_id = int(warehouse_id)
            base_query = base_query.filter(InventoryMove.location_id == warehouse_id)
        except (TypeError, ValueError):
            return None, "رقم المخزن غير صالح."

    start_dt = parse_date(date_from) if date_from else None
    end_dt = parse_date_end(date_to) if date_to else None

    range_query = base_query
    if start_dt:
        range_query = range_query.filter(InventoryMove.date >= start_dt)
    if end_dt:
        range_query = range_query.filter(InventoryMove.date <= end_dt)

    totals_row = _get_item_movement_totals(range_query)

    opening_balance = Decimal("0")
    if start_dt:
        opening_value = (
            base_query.filter(InventoryMove.date < start_dt)
            .with_entities(func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0))
            .scalar()
        )
        opening_balance = Decimal(str(opening_value or 0))

    total_balance_value = (
        range_query.with_entities(func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0)).scalar()
        or 0
    )
    total_warehouses_balance = Decimal(str(total_balance_value))

    warehouse_rows = (
        range_query.with_entities(
            Location.name.label("warehouse_name"),
            func.coalesce(func.sum(InventoryMove.qty_in - InventoryMove.qty_out), 0).label("balance"),
            func.coalesce(func.sum(InventoryMove.qty_in), 0).label("incoming_volume"),
        )
        .join(Location, InventoryMove.location_id == Location.id)
        .group_by(Location.name)
        .order_by(Location.name.asc())
        .all()
    )
    purchase_price = Decimal(str(item.purchase_price or 0)) if item else Decimal("0")
    warehouse_balances = []
    for row in warehouse_rows:
        balance = Decimal(str(row.balance or 0))
        incoming_volume = Decimal(str(row.incoming_volume or 0))
        warehouse_balances.append(
            {
                "warehouse_name": row.warehouse_name,
                "balance": _decimal_to_float(balance),
                "incoming_volume": _decimal_to_float(incoming_volume),
                "qty": _decimal_to_float(balance),
                "cost": _decimal_to_float(balance * purchase_price),
            }
        )

    response = {
        "totals": {
            "sales_total": _decimal_to_float(Decimal(str(totals_row.sales_total or 0))),
            "purchases_total": _decimal_to_float(Decimal(str(totals_row.purchases_total or 0))),
            "purchase_returns_total": _decimal_to_float(Decimal(str(totals_row.purchase_returns_total or 0))),
            "sales_returns_total": _decimal_to_float(Decimal(str(totals_row.sales_returns_total or 0))),
            "waste_total": _decimal_to_float(Decimal(str(totals_row.waste_total or 0))),
            "shortage_total": _decimal_to_float(Decimal(str(totals_row.shortage_total or 0))),
            "damage_total": _decimal_to_float(Decimal(str(totals_row.damage_total or 0))),
            "increase_total": _decimal_to_float(Decimal(str(totals_row.increase_total or 0))),
        },
        "balances_summary": {
            "opening_balance": _decimal_to_float(opening_balance),
            "total_warehouses_balance": _decimal_to_float(total_warehouses_balance),
            "public_price": _decimal_to_float(Decimal(str(item.sale_price or 0)) if item else Decimal("0")),
        },
        "warehouse_balances": warehouse_balances,
        "meta": {
            "item_id": item_id,
            "customer_id": customer_id,
            "warehouse_id": warehouse_id,
            "date_from": date_from,
            "date_to": date_to,
        },
    }
    return response, ""


@app.get("/api/lookups/items")
async def api_lookup_items(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Item).order_by(Item.name.asc()).all()
    return JSONResponse(
        [
            {"id": r.id, "name": r.name, "public_price": float(r.sale_price or 0)}
            for r in rows
        ]
    )


@app.get("/api/lookups/suppliers")
async def api_lookup_suppliers(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Supplier).order_by(Supplier.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/categories")
async def api_lookup_categories(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(ItemCategory).order_by(ItemCategory.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/customers")
async def api_lookup_customers(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Location).filter(Location.type == "pharmacy").order_by(Location.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/warehouses")
async def api_lookup_warehouses(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = (
        db.query(Location)
        .filter(Location.type.in_(["warehouse", "sub_warehouse"]))
        .order_by(Location.name.asc())
        .all()
    )
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.post("/api/reports/item-movement")
async def api_item_movement_report(request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    data, error = _build_item_movement_report(db, payload or {})
    if error:
        return JSONResponse({"error": error}, status_code=400)
    return JSONResponse(data)


@app.post("/api/reports/item-movement/{view_type}")
async def api_item_movement_report_view(view_type: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    data, error = _build_item_movement_report(db, payload or {})
    if error:
        return JSONResponse({"error": error}, status_code=400)

    view_map = {
        "sales-pricing": {"types": ["sale"], "title": "تسعير مبيعات"},
        "waste": {"types": ["damage", "damages"], "title": "هالك"},
        "purchase-returns": {"types": ["purchase_return"], "title": "مردودات مشتريات"},
        "sales-returns": {"types": ["sale_return"], "title": "مردودات مبيعات"},
        "purchases": {"types": ["purchase"], "title": "المشتريات"},
        "sales": {"types": ["sale"], "title": "المبيعات"},
    }
    if view_type not in view_map:
        return JSONResponse({"error": "نوع التقرير غير مدعوم."}, status_code=400)

    base_query = db.query(InventoryMove).filter(InventoryMove.item_id == int(payload.get("item_id")))
    if payload.get("warehouse_id"):
        base_query = base_query.filter(InventoryMove.location_id == int(payload.get("warehouse_id")))
    if payload.get("date_from"):
        base_query = base_query.filter(InventoryMove.date >= parse_date(payload.get("date_from")))
    if payload.get("date_to"):
        base_query = base_query.filter(InventoryMove.date <= parse_date_end(payload.get("date_to")))

    base_query = base_query.filter(InventoryMove.source_type.in_(view_map[view_type]["types"]))
    rows = (
        base_query.join(Location, InventoryMove.location_id == Location.id)
        .with_entities(
            InventoryMove.date,
            InventoryMove.source_type,
            InventoryMove.qty_in,
            InventoryMove.qty_out,
            InventoryMove.source_id,
            InventoryMove.notes,
            Location.name.label("warehouse_name"),
        )
        .order_by(InventoryMove.date.desc())
        .limit(200)
        .all()
    )
    table_rows = []
    for row in rows:
        qty_in = Decimal(str(row.qty_in or 0))
        qty_out = Decimal(str(row.qty_out or 0))
        qty = qty_in if qty_in > 0 else qty_out
        table_rows.append(
            [
                row.date.strftime("%Y-%m-%d") if row.date else "",
                f"{row.source_id}" if row.source_id else "-",
                _decimal_to_float(qty),
                row.warehouse_name or "",
                row.notes or "-",
            ]
        )

    return JSONResponse(
        {
            "view_type": view_type,
            "columns": ["التاريخ", "رقم العملية", "الكمية", "المخزن", "ملاحظات"],
            "rows": table_rows,
        }
    )


@app.get("/api/lookups/safes")
async def api_lookup_safes(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(CashAccount).order_by(CashAccount.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/employees")
async def api_lookup_employees(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Representative).order_by(Representative.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/doctors")
async def api_lookup_doctors(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Doctor).order_by(Doctor.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/pharmacies")
async def api_lookup_pharmacies(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Location).filter(Location.type == "pharmacy").order_by(Location.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.post("/api/treasury/reports/{report_key}")
async def api_treasury_reports(report_key: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    report_titles = {
        "cash-receipts-list": "قائمة أذون إستلام نقدية",
        "cash-payments-list": "قائمة أذون دفع نقدية",
        "safe-movement": "حركة خزنة",
        "safes-movement": "حركة الخزائن",
        "safe-register-employees": "حركة سجل الخزنة - موظفين",
        "safes-summary": "تقرير الخزائن",
    }
    if report_key not in report_titles:
        return JSONResponse({"error": "نوع التقرير غير مدعوم."}, status_code=400)

    title_ar = report_titles[report_key]
    rows = []
    totals = {}
    columns = []

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    safe_id = payload.get("safe_id")
    doc_no = payload.get("doc_no")
    employee_id = payload.get("employee_id")

    start_dt = parse_date(date_from) if date_from else None
    end_dt = parse_date_end(date_to) if date_to else None

    def _apply_date_filter(query, column):
        if start_dt:
            query = query.filter(column >= start_dt)
        if end_dt:
            query = query.filter(column <= end_dt)
        return query

    def _safe_description(tx: CashTransaction) -> str:
        if tx.type == "salary_payment":
            if tx.date:
                return f"صرف رواتب شهر {tx.date.month}/{tx.date.year}"
            return "صرف رواتب"
        if tx.notes and ("??" in tx.notes or "\ufffd" in tx.notes):
            return ""
        return tx.notes or ""

    if report_key == "cash-receipts-list":
        columns = [
            {"key": "doc_no", "label_ar": "رقم الإذن"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "safe_name", "label_ar": "الخزنة"},
            {"key": "received_from", "label_ar": "المستلم/العميل"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "amount", "label_ar": "المبلغ"},
            {"key": "reference", "label_ar": "طريقة الدفع/مرجع"},
        ]
        query = (
            db.query(CashTransaction, CashAccount, Transfer, Location)
            .join(CashAccount, CashTransaction.to_account_id == CashAccount.id)
            .outerjoin(Transfer, CashTransaction.source_id == Transfer.id)
            .outerjoin(Location, Transfer.to_location_id == Location.id)
            .filter(CashTransaction.type == "collection")
        )
        if safe_id:
            query = query.filter(CashTransaction.to_account_id == int(safe_id))
        if doc_no and str(doc_no).isdigit():
            doc_id = int(doc_no)
            query = query.filter((CashTransaction.source_id == doc_id) | (CashTransaction.id == doc_id))
        query = _apply_date_filter(query, CashTransaction.date)
        query = query.order_by(CashTransaction.date.desc())
        for tx, safe, sale, customer in query.all():
            rows.append(
                {
                    "doc_no": str(tx.source_id or tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "safe_name": safe.name if safe else "",
                    "received_from": customer.name if customer else "",
                    "description": tx.notes or "",
                    "amount": float(tx.amount or 0),
                    "reference": tx.source_type or "",
                }
            )
        totals = {"amount_sum": float(sum((r["amount"] for r in rows), 0.0))}
    elif report_key == "cash-payments-list":
        columns = [
            {"key": "doc_no", "label_ar": "رقم الإذن"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "safe_name", "label_ar": "الخزنة"},
            {"key": "paid_to", "label_ar": "المستفيد"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "amount", "label_ar": "المبلغ"},
            {"key": "category", "label_ar": "التصنيف"},
        ]
        query = (
            db.query(CashTransaction, CashAccount, Purchase, Supplier, Doctor)
            .outerjoin(CashAccount, CashTransaction.from_account_id == CashAccount.id)
            .outerjoin(Purchase, CashTransaction.source_id == Purchase.id)
            .outerjoin(Supplier, Purchase.supplier_id == Supplier.id)
            .outerjoin(Doctor, CashTransaction.source_id == Doctor.id)
            .filter(CashTransaction.type.in_(["supplier_payment", "doctor_payment", "other_expense", "payment"]))
        )
        if safe_id:
            query = query.filter(CashTransaction.from_account_id == int(safe_id))
        if doc_no and str(doc_no).isdigit():
            doc_id = int(doc_no)
            query = query.filter((CashTransaction.source_id == doc_id) | (CashTransaction.id == doc_id))
        query = _apply_date_filter(query, CashTransaction.date)
        query = query.order_by(CashTransaction.date.desc())
        for tx, safe, purchase, supplier, doctor in query.all():
            paid_to = ""
            category = tx.type
            if tx.type == "supplier_payment" and supplier:
                paid_to = supplier.name
                category = "supplier"
            elif tx.type == "doctor_payment" and doctor:
                paid_to = doctor.name
                category = "doctor"
            rows.append(
                {
                    "doc_no": str(tx.source_id or tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "safe_name": safe.name if safe else "",
                    "paid_to": paid_to,
                    "description": _safe_description(tx),
                    "amount": float(tx.amount or 0),
                    "category": category,
                }
            )
        totals = {"amount_sum": float(sum((r["amount"] for r in rows), 0.0))}
    elif report_key == "safe-movement":
        columns = [
            {"key": "txn_date", "label_ar": "التاريخ"},
            {"key": "txn_type", "label_ar": "نوع الحركة"},
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "amount_in", "label_ar": "داخل"},
            {"key": "amount_out", "label_ar": "خارج"},
            {"key": "balance", "label_ar": "الرصيد"},
        ]
        if not safe_id:
            return JSONResponse({"error": "اختر الخزنة أولاً."}, status_code=400)
        safe_id_val = int(safe_id)
        base_query = db.query(CashTransaction).filter(
            (CashTransaction.from_account_id == safe_id_val)
            | (CashTransaction.to_account_id == safe_id_val)
        )
        if start_dt:
            base_query = base_query.filter(CashTransaction.date >= start_dt)
        if end_dt:
            base_query = base_query.filter(CashTransaction.date <= end_dt)

        opening_query = db.query(CashTransaction).filter(
            (CashTransaction.from_account_id == safe_id_val)
            | (CashTransaction.to_account_id == safe_id_val)
        )
        if start_dt:
            opening_query = opening_query.filter(CashTransaction.date < start_dt)
        incoming = (
            opening_query.filter(CashTransaction.to_account_id == safe_id_val)
            .with_entities(func.coalesce(func.sum(CashTransaction.amount), 0))
            .scalar()
            or 0
        )
        outgoing = (
            opening_query.filter(CashTransaction.from_account_id == safe_id_val)
            .with_entities(func.coalesce(func.sum(CashTransaction.amount), 0))
            .scalar()
            or 0
        )
        balance = Decimal(str(incoming)) - Decimal(str(outgoing))

        total_in = Decimal("0")
        total_out = Decimal("0")
        for tx in base_query.order_by(CashTransaction.date.asc(), CashTransaction.id.asc()).all():
            amount_in = Decimal(str(tx.amount or 0)) if tx.to_account_id == safe_id_val else Decimal("0")
            amount_out = Decimal(str(tx.amount or 0)) if tx.from_account_id == safe_id_val else Decimal("0")
            balance += amount_in - amount_out
            total_in += amount_in
            total_out += amount_out
            rows.append(
                {
                    "txn_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "txn_type": tx.type,
                    "doc_no": str(tx.source_id or tx.id),
                    "description": _safe_description(tx),
                    "amount_in": float(amount_in),
                    "amount_out": float(amount_out),
                    "balance": float(balance),
                }
            )
        totals = {"total_in": float(total_in), "total_out": float(total_out), "ending_balance": float(balance)}
    elif report_key == "safes-movement":
        columns = [
            {"key": "transfer_date", "label_ar": "التاريخ"},
            {"key": "transfer_no", "label_ar": "رقم التحويل"},
            {"key": "safe_from", "label_ar": "من خزنة"},
            {"key": "safe_to", "label_ar": "إلى خزنة"},
            {"key": "amount", "label_ar": "المبلغ"},
            {"key": "notes", "label_ar": "ملاحظات"},
        ]
        safe_from = aliased(CashAccount)
        safe_to = aliased(CashAccount)
        query = (
            db.query(CashTransaction, safe_from, safe_to)
            .join(safe_from, CashTransaction.from_account_id == safe_from.id)
            .join(safe_to, CashTransaction.to_account_id == safe_to.id)
            .filter(CashTransaction.type == "transfer")
        )
        if safe_id:
            safe_id_val = int(safe_id)
            query = query.filter(
                (CashTransaction.from_account_id == safe_id_val)
                | (CashTransaction.to_account_id == safe_id_val)
            )
        query = _apply_date_filter(query, CashTransaction.date)
        query = query.order_by(CashTransaction.date.desc())
        for tx, safe_from, safe_to in query.all():
            rows.append(
                {
                    "transfer_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "transfer_no": str(tx.id),
                    "safe_from": safe_from.name if safe_from else "",
                    "safe_to": safe_to.name if safe_to else "",
                    "amount": float(tx.amount or 0),
                    "notes": tx.notes or "",
                }
            )
        totals = {"amount_sum": float(sum((r["amount"] for r in rows), 0.0))}
    elif report_key == "safe-register-employees":
        columns = [
            {"key": "txn_date", "label_ar": "التاريخ"},
            {"key": "employee_name", "label_ar": "الموظف"},
            {"key": "txn_type", "label_ar": "نوع الحركة"},
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "amount_in", "label_ar": "داخل"},
            {"key": "amount_out", "label_ar": "خارج"},
            {"key": "balance", "label_ar": "الرصيد"},
        ]
        query = db.query(EmployeeSalary, Representative).join(Representative, EmployeeSalary.rep_id == Representative.id)
        if employee_id:
            query = query.filter(EmployeeSalary.rep_id == int(employee_id))
        if start_dt:
            query = query.filter(EmployeeSalary.date >= start_dt.date())
        if end_dt:
            query = query.filter(EmployeeSalary.date <= end_dt.date())
        query = query.order_by(EmployeeSalary.date.asc(), EmployeeSalary.id.asc())
        balance = Decimal("0")
        total_in = Decimal("0")
        total_out = Decimal("0")
        for sal, rep in query.all():
            amount_out = Decimal(str(sal.amount or 0))
            balance -= amount_out
            total_out += amount_out
            rows.append(
                {
                    "txn_date": sal.date.strftime("%Y-%m-%d") if sal.date else "",
                    "employee_name": rep.name if rep else "",
                    "txn_type": "salary_payment",
                    "doc_no": str(sal.id),
                    "amount_in": 0.0,
                    "amount_out": float(amount_out),
                    "balance": float(balance),
                }
            )
        totals = {"total_in": float(total_in), "total_out": float(total_out), "ending_balance": float(balance)}
    elif report_key == "safes-summary":
        columns = [
            {"key": "safe_name", "label_ar": "الخزنة"},
            {"key": "opening_balance", "label_ar": "رصيد أول المدة"},
            {"key": "total_in", "label_ar": "إجمالي داخل"},
            {"key": "total_out", "label_ar": "إجمالي خارج"},
            {"key": "current_balance", "label_ar": "الرصيد الحالي"},
        ]
        accounts = db.query(CashAccount).order_by(CashAccount.name.asc()).all()
        for acc in accounts:
            tx_query = db.query(CashTransaction).filter(
                (CashTransaction.from_account_id == acc.id) | (CashTransaction.to_account_id == acc.id)
            )
            if start_dt:
                tx_query = tx_query.filter(CashTransaction.date >= start_dt)
            if end_dt:
                tx_query = tx_query.filter(CashTransaction.date <= end_dt)
            incoming = (
                tx_query.filter(CashTransaction.to_account_id == acc.id)
                .with_entities(func.coalesce(func.sum(CashTransaction.amount), 0))
                .scalar()
                or 0
            )
            outgoing = (
                tx_query.filter(CashTransaction.from_account_id == acc.id)
                .with_entities(func.coalesce(func.sum(CashTransaction.amount), 0))
                .scalar()
                or 0
            )
            opening_in = (
                db.query(CashTransaction)
                .filter(CashTransaction.to_account_id == acc.id)
                .filter(CashTransaction.date < start_dt)
                .with_entities(func.coalesce(func.sum(CashTransaction.amount), 0))
                .scalar()
                if start_dt
                else 0
            ) or 0
            opening_out = (
                db.query(CashTransaction)
                .filter(CashTransaction.from_account_id == acc.id)
                .filter(CashTransaction.date < start_dt)
                .with_entities(func.coalesce(func.sum(CashTransaction.amount), 0))
                .scalar()
                if start_dt
                else 0
            ) or 0
            opening_balance = Decimal(str(opening_in)) - Decimal(str(opening_out))
            current_balance = opening_balance + Decimal(str(incoming)) - Decimal(str(outgoing))
            rows.append(
                {
                    "safe_name": acc.name,
                    "opening_balance": float(opening_balance),
                    "total_in": float(incoming),
                    "total_out": float(outgoing),
                    "current_balance": float(current_balance),
                }
            )
        totals = {"current_balance": float(sum((r["current_balance"] for r in rows), 0.0))}

    return JSONResponse({"title_ar": title_ar, "columns": columns, "rows": rows, "totals": totals})


@app.get("/api/lookups/reps")
async def api_lookup_reps(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Representative).order_by(Representative.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/companies")
async def api_lookup_companies(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Location).order_by(Location.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.get("/api/lookups/branches")
async def api_lookup_branches(user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return JSONResponse([])


@app.get("/api/lookups/vendors")
async def api_lookup_vendors(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    rows = db.query(Supplier).order_by(Supplier.name.asc()).all()
    return JSONResponse([{"id": r.id, "name": r.name} for r in rows])


@app.post("/api/employees/reports/{report_key}")
async def api_employee_reports(report_key: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    report_titles = {
        "emp-adjustments": "كشف خصومات وإضافات",
        "rep-sales-returns": "قائمة مبيعات و مرتجعات مندوب",
        "reps-sales-returns": "قائمة مبيعات و مرتجعات المندوبين",
        "rep-sales-by-company": "مبيعات مندوب لشركة",
        "rep-item-sales": "تقرير مبيعات أصناف مندوب",
    }
    if report_key not in report_titles:
        return JSONResponse({"error": "نوع تقرير غير صالح."}, status_code=400)

    title_ar = report_titles[report_key]
    columns = []
    rows = []
    totals = {}

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    start_date = parse_date_only(date_from) if date_from else None
    end_date = parse_date_only(date_to) if date_to else None

    if report_key == "emp-adjustments":
        columns = [
            {"key": "employee_name", "label_ar": "الموظف"},
            {"key": "txn_date", "label_ar": "التاريخ"},
            {"key": "adjustment_type", "label_ar": "نوع الحركة"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "amount", "label_ar": "المبلغ"},
            {"key": "reference_no", "label_ar": "رقم المرجع"},
        ]
        adjustment_type = (payload or {}).get("adjustment_type") or "all"
        additions = db.query(EmployeeAddition).join(Representative).all()
        deductions = db.query(EmployeeDeduction).join(Representative).all()
        rows = []
        for row in additions:
            if start_date and row.date < start_date:
                continue
            if end_date and row.date > end_date:
                continue
            if adjustment_type in {"deduction"}:
                continue
            rows.append(
                {
                    "employee_name": row.rep.name if row.rep else "",
                    "txn_date": row.date.strftime("%Y-%m-%d") if row.date else "",
                    "adjustment_type": "addition",
                    "description": row.reason or row.notes or "",
                    "amount": float(row.amount or 0),
                    "reference_no": str(row.id),
                }
            )
        for row in deductions:
            if start_date and row.date < start_date:
                continue
            if end_date and row.date > end_date:
                continue
            if adjustment_type in {"addition"}:
                continue
            rows.append(
                {
                    "employee_name": row.rep.name if row.rep else "",
                    "txn_date": row.date.strftime("%Y-%m-%d") if row.date else "",
                    "adjustment_type": "deduction",
                    "description": row.reason or row.notes or "",
                    "amount": float(row.amount or 0),
                    "reference_no": str(row.id),
                }
            )
        totals = {"amount_sum": float(sum((r["amount"] for r in rows), 0.0))}
    elif report_key == "rep-sales-returns":
        columns = [
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "doc_type", "label_ar": "نوع المستند"},
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "net_amount", "label_ar": "الصافي"},
            {"key": "tax_amount", "label_ar": "الضريبة"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
        ]
        rep_id = payload.get("rep_id")
        include_returns = payload.get("include_returns", True)
        if not rep_id:
            return JSONResponse({"error": "برجاء اختيار مندوب."}, status_code=400)
        kinds = ["sale_return", "sale"] if include_returns else ["sale"]
        query = (
            db.query(Transfer, Location)
            .join(Location, Transfer.to_location_id == Location.id)
            .filter(Transfer.kind.in_(kinds), Transfer.rep_id == int(rep_id))
        )
        if start_date:
            query = query.filter(Transfer.date >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            query = query.filter(Transfer.date <= datetime.combine(end_date, datetime.max.time()))
        for sale, customer in query.order_by(Transfer.date.desc()).all():
            total = Decimal(str(sale.total or 0))
            multiplier = Decimal("-1") if sale.kind == "sale_return" else Decimal("1")
            net = total * multiplier
            rows.append(
                {
                    "doc_date": sale.date.strftime("%Y-%m-%d") if sale.date else "",
                    "doc_type": "return" if sale.kind == "sale_return" else "sale",
                    "doc_no": str(sale.id),
                    "customer_name": customer.name if customer else "",
                    "net_amount": float(net),
                    "tax_amount": 0.0,
                    "total_amount": float(net),
                }
            )
        totals = {
            "net_sum": float(sum((r["net_amount"] for r in rows), 0.0)),
            "total_sum": float(sum((r["total_amount"] for r in rows), 0.0)),
        }
    elif report_key == "reps-sales-returns":
        columns = [
            {"key": "rep_name", "label_ar": "المندوب"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "doc_type", "label_ar": "نوع المستند"},
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "net_amount", "label_ar": "الصافي"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
        ]
        rep_id = payload.get("rep_id")
        include_returns = payload.get("include_returns", True)
        kinds = ["sale_return", "sale"] if include_returns else ["sale"]
        query = (
            db.query(Transfer, Location, Representative)
            .join(Location, Transfer.to_location_id == Location.id)
            .outerjoin(Representative, Transfer.rep_id == Representative.id)
            .filter(Transfer.kind.in_(kinds))
        )
        if rep_id:
            query = query.filter(Transfer.rep_id == int(rep_id))
        if start_date:
            query = query.filter(Transfer.date >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            query = query.filter(Transfer.date <= datetime.combine(end_date, datetime.max.time()))
        for sale, customer, rep in query.order_by(Transfer.date.desc()).all():
            total = Decimal(str(sale.total or 0))
            multiplier = Decimal("-1") if sale.kind == "sale_return" else Decimal("1")
            net = total * multiplier
            rows.append(
                {
                    "rep_name": rep.name if rep else "",
                    "doc_date": sale.date.strftime("%Y-%m-%d") if sale.date else "",
                    "doc_type": "return" if sale.kind == "sale_return" else "sale",
                    "doc_no": str(sale.id),
                    "customer_name": customer.name if customer else "",
                    "net_amount": float(net),
                    "total_amount": float(net),
                }
            )
        totals = {
            "net_sum": float(sum((r["net_amount"] for r in rows), 0.0)),
            "total_sum": float(sum((r["total_amount"] for r in rows), 0.0)),
        }
    elif report_key == "rep-sales-by-company":
        columns = [
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "net_amount", "label_ar": "الصافي"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
        ]
        rep_id = payload.get("rep_id")
        company_id = payload.get("company_id")
        if not rep_id or not company_id:
            return JSONResponse({"error": "برجاء اختيار مندوب وشركة."}, status_code=400)
        query = (
            db.query(Transfer, Location)
            .join(Location, Transfer.to_location_id == Location.id)
            .filter(Transfer.kind == "sale", Transfer.rep_id == int(rep_id), Transfer.to_location_id == int(company_id))
        )
        if start_date:
            query = query.filter(Transfer.date >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            query = query.filter(Transfer.date <= datetime.combine(end_date, datetime.max.time()))
        for sale, customer in query.order_by(Transfer.date.desc()).all():
            total = Decimal(str(sale.total or 0))
            rows.append(
                {
                    "doc_date": sale.date.strftime("%Y-%m-%d") if sale.date else "",
                    "doc_no": str(sale.id),
                    "customer_name": customer.name if customer else "",
                    "net_amount": float(total),
                    "total_amount": float(total),
                }
            )
        totals = {
            "net_sum": float(sum((r["net_amount"] for r in rows), 0.0)),
            "total_sum": float(sum((r["total_amount"] for r in rows), 0.0)),
        }
    elif report_key == "rep-item-sales":
        columns = [
            {"key": "item_code", "label_ar": "كود الصنف"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty", "label_ar": "الكمية"},
            {"key": "unit_price_avg", "label_ar": "متوسط السعر"},
            {"key": "net_amount", "label_ar": "الصافي"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
        ]
        rep_id = payload.get("rep_id")
        if not rep_id:
            return JSONResponse({"error": "برجاء اختيار مندوب."}, status_code=400)
        item_id = payload.get("item_id")
        query = (
            db.query(
                Item.id,
                Item.name,
                func.coalesce(func.sum(TransferLine.requested_qty), 0).label("qty"),
                func.coalesce(func.avg(TransferLine.unit_price), 0).label("avg_price"),
                func.coalesce(func.sum(TransferLine.line_total), 0).label("total"),
            )
            .join(Transfer, TransferLine.transfer_id == Transfer.id)
            .join(Item, TransferLine.item_id == Item.id)
            .filter(Transfer.kind == "sale", Transfer.rep_id == int(rep_id))
        )
        if item_id:
            query = query.filter(TransferLine.item_id == int(item_id))
        if start_date:
            query = query.filter(Transfer.date >= datetime.combine(start_date, datetime.min.time()))
        if end_date:
            query = query.filter(Transfer.date <= datetime.combine(end_date, datetime.max.time()))
        for row in query.group_by(Item.id, Item.name).order_by(Item.name.asc()).all():
            rows.append(
                {
                    "item_code": str(row.id),
                    "item_name": row.name,
                    "qty": float(row.qty or 0),
                    "unit_price_avg": float(row.avg_price or 0),
                    "net_amount": float(row.total or 0),
                    "total_amount": float(row.total or 0),
                }
            )
        totals = {
            "qty_sum": float(sum((r["qty"] for r in rows), 0.0)),
            "net_sum": float(sum((r["net_amount"] for r in rows), 0.0)),
            "total_sum": float(sum((r["total_amount"] for r in rows), 0.0)),
        }

    return JSONResponse({"title_ar": title_ar, "columns": columns, "rows": rows, "totals": totals})


@app.get("/api/lookups/invoice-types")
async def api_lookup_invoice_types(user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return JSONResponse(
        [
            {"id": "sale", "name": "مبيعات"},
            {"id": "sale_return", "name": "مرتجع مبيعات"},
            {"id": "purchase", "name": "مشتريات"},
            {"id": "purchase_return", "name": "مرتجع مشتريات"},
        ]
    )


@app.post("/api/customers/reports/{report_key}")
async def api_customers_reports(report_key: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    report_titles = {
        "customers-list": "قائمة العملاء",
        "customer-activity-volume": "حجم تعامل عميل",
        "customers-balances": "أرصدة العملاء",
        "customer-statement": "كشف حساب عميل",
        "customers-statement": "كشف حساب عملاء",
    }
    if report_key not in report_titles:
        return JSONResponse({"error": "نوع تقرير غير صالح."}, status_code=400)

    title_ar = report_titles[report_key]
    columns = []
    rows = []
    totals = {}
    sections = {}

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    as_of_date = payload.get("as_of_date")
    customer_id = payload.get("customer_id")
    customer_from_id = payload.get("customer_from_id")
    customer_to_id = payload.get("customer_to_id")
    company_id = payload.get("company_id")
    warehouse_id = payload.get("warehouse_id")
    invoice_type = payload.get("invoice_type")
    sort_by = payload.get("sort_by") or "date"
    sort_dir = payload.get("sort_dir") or "asc"
    balance_side = payload.get("balance_side") or "all"
    search_text = payload.get("search_text")
    group_by = payload.get("group_by")

    start_dt = parse_date(date_from) if date_from else None
    end_dt = parse_date_end(date_to) if date_to else None
    as_of_dt = parse_date_end(as_of_date) if as_of_date else None

    def _balance_desc(value: Decimal) -> str:
        if value > 0:
            return "مدين"
        if value < 0:
            return "دائن"
        return "متوازن"

    if report_key == "customers-list":
        columns = [
            {"key": "customer_id", "label_ar": "رقم العميل"},
            {"key": "customer_name", "label_ar": "اسم العميل"},
            {"key": "balance_desc", "label_ar": "نوع الرصيد"},
            {"key": "debit_total", "label_ar": "مدين"},
            {"key": "credit_total", "label_ar": "دائن"},
            {"key": "dept_total", "label_ar": "الرصيد"},
            {"key": "phone", "label_ar": "الهاتف"},
            {"key": "area", "label_ar": "المنطقة"},
        ]
        tx_sub = (
            db.query(
                LocationTransaction.location_id.label("location_id"),
                func.coalesce(
                    func.sum(case((LocationTransaction.amount > 0, LocationTransaction.amount), else_=0)), 0
                ).label("debit_total"),
                func.coalesce(
                    func.sum(case((LocationTransaction.amount < 0, -LocationTransaction.amount), else_=0)), 0
                ).label("credit_total"),
                func.coalesce(func.sum(LocationTransaction.amount), 0).label("balance"),
            )
            .group_by(LocationTransaction.location_id)
            .subquery()
        )
        query = (
            db.query(Location, tx_sub.c.debit_total, tx_sub.c.credit_total, tx_sub.c.balance)
            .outerjoin(tx_sub, Location.id == tx_sub.c.location_id)
        )
        query = query.filter(Location.type == "pharmacy")
        if search_text:
            like = f"%{search_text.strip()}%"
            query = query.filter(Location.name.ilike(like))
        if company_id and str(company_id).isdigit():
            query = query.filter(Location.id == int(company_id))
        totals_debit = Decimal("0")
        totals_credit = Decimal("0")
        totals_balance = Decimal("0")
        for loc, debit_val, credit_val, balance_val in query.order_by(Location.name.asc()).all():
            debit = Decimal(str(debit_val or 0))
            credit = Decimal(str(credit_val or 0))
            balance = Decimal(str(balance_val or 0))
            totals_debit += debit
            totals_credit += credit
            totals_balance += balance
            rows.append(
                {
                    "customer_id": str(loc.id),
                    "customer_name": loc.name,
                    "balance_desc": _balance_desc(balance),
                    "debit_total": float(debit),
                    "credit_total": float(credit),
                    "dept_total": float(balance),
                    "phone": loc.phone or "",
                    "area": loc.region or "",
                }
            )
        totals = {
            "debit_sum": float(totals_debit),
            "credit_sum": float(totals_credit),
            "ending_balance": float(totals_balance),
            "customers_count": len(rows),
        }
    elif report_key == "customer-activity-volume":
        columns = [
            {"key": "metric", "label_ar": "البند"},
            {"key": "value", "label_ar": "القيمة"},
        ]
        if not customer_id:
            return JSONResponse({"error": "برجاء اختيار العميل."}, status_code=400)
        tx_query = db.query(LocationTransaction).filter(LocationTransaction.location_id == int(customer_id))
        if start_dt:
            tx_query = tx_query.filter(LocationTransaction.date >= start_dt)
        if end_dt:
            tx_query = tx_query.filter(LocationTransaction.date <= end_dt)
        txs = tx_query.all()
        sales_amount = sum((Decimal(str(t.amount or 0)) for t in txs if t.source_type == "sale"), Decimal("0"))
        sales_returns_amount = sum((abs(Decimal(str(t.amount or 0))) for t in txs if t.source_type == "sale_return"), Decimal("0"))
        cash_receipts_amount = sum((abs(Decimal(str(t.amount or 0))) for t in txs if t.type == "payment"), Decimal("0"))
        discounts_amount = sum((abs(Decimal(str(t.amount or 0))) for t in txs if t.type == "adjustment" and Decimal(str(t.amount or 0)) < 0), Decimal("0"))
        additions_amount = sum((Decimal(str(t.amount or 0)) for t in txs if t.type == "adjustment" and Decimal(str(t.amount or 0)) > 0), Decimal("0"))

        opening_balance = Decimal("0")
        if start_dt:
            opening_balance = Decimal(
                str(
                    db.query(func.coalesce(func.sum(LocationTransaction.amount), 0))
                    .filter(LocationTransaction.location_id == int(customer_id))
                    .filter(LocationTransaction.date < start_dt)
                    .scalar()
                    or 0
                )
            )
        ending_balance = opening_balance + sum((Decimal(str(t.amount or 0)) for t in txs), Decimal("0"))
        account_balance = Decimal(
            str(
                db.query(func.coalesce(func.sum(LocationTransaction.amount), 0))
                .filter(LocationTransaction.location_id == int(customer_id))
                .scalar()
                or 0
            )
        )
        customer = db.query(Location).filter(Location.id == int(customer_id)).first()
        sections = {
            "analysis": {
                "sales_amount": float(sales_amount),
                "sales_returns_amount": float(sales_returns_amount),
                "purchases_amount": 0.0,
                "purchase_returns_amount": 0.0,
                "cash_receipts_amount": float(cash_receipts_amount),
                "cash_payments_amount": 0.0,
                "discounts_amount": float(discounts_amount),
                "additions_amount": float(additions_amount),
                "opening_balance": float(opening_balance),
                "ending_balance": float(ending_balance),
            },
            "account_info": {
                "account_no": str(customer.id) if customer else "",
                "customer_name": customer.name if customer else "",
                "status": "???",
                "account_limit": 0.0,
                "current_balance": float(account_balance),
                "balance_desc": _balance_desc(account_balance),
            },
        }
        rows = [
            {"metric": "مبيعات", "value": float(sales_amount)},
            {"metric": "مرتجع مبيعات", "value": float(sales_returns_amount)},
            {"metric": "تحصيلات نقدية", "value": float(cash_receipts_amount)},
            {"metric": "خصومات", "value": float(discounts_amount)},
            {"metric": "إضافات", "value": float(additions_amount)},
        ]
    elif report_key == "customers-balances":
        columns = [
            {"key": "customer_id", "label_ar": "رقم العميل"},
            {"key": "customer_name", "label_ar": "اسم العميل"},
            {"key": "opening_balance", "label_ar": "رصيد أول المدة"},
            {"key": "debit", "label_ar": "مدين"},
            {"key": "credit", "label_ar": "دائن"},
            {"key": "closing_balance", "label_ar": "رصيد آخر المدة"},
            {"key": "balance_desc", "label_ar": "نوع الرصيد"},
        ]
        location_query = db.query(Location).filter(Location.type == "pharmacy")
        if customer_from_id and str(customer_from_id).isdigit():
            location_query = location_query.filter(Location.id >= int(customer_from_id))
        if customer_to_id and str(customer_to_id).isdigit():
            location_query = location_query.filter(Location.id <= int(customer_to_id))
        locations = location_query.order_by(Location.id.asc()).all()
        opening_sum = Decimal("0")
        debit_sum = Decimal("0")
        credit_sum = Decimal("0")
        closing_sum = Decimal("0")
        for loc in locations:
            if as_of_dt:
                opening_balance = Decimal("0")
                debit_val = Decimal(
                    str(
                        db.query(func.coalesce(func.sum(case((LocationTransaction.amount > 0, LocationTransaction.amount), else_=0)), 0))
                        .filter(LocationTransaction.location_id == loc.id)
                        .filter(LocationTransaction.date <= as_of_dt)
                        .scalar()
                        or 0
                    )
                )
                credit_val = Decimal(
                    str(
                        db.query(func.coalesce(func.sum(case((LocationTransaction.amount < 0, -LocationTransaction.amount), else_=0)), 0))
                        .filter(LocationTransaction.location_id == loc.id)
                        .filter(LocationTransaction.date <= as_of_dt)
                        .scalar()
                        or 0
                    )
                )
                closing_balance = Decimal(
                    str(
                        db.query(func.coalesce(func.sum(LocationTransaction.amount), 0))
                        .filter(LocationTransaction.location_id == loc.id)
                        .filter(LocationTransaction.date <= as_of_dt)
                        .scalar()
                        or 0
                    )
                )
            else:
                opening_balance = Decimal("0")
                if start_dt:
                    opening_balance = Decimal(
                        str(
                            db.query(func.coalesce(func.sum(LocationTransaction.amount), 0))
                            .filter(LocationTransaction.location_id == loc.id)
                            .filter(LocationTransaction.date < start_dt)
                            .scalar()
                            or 0
                        )
                    )
                range_query = db.query(LocationTransaction).filter(LocationTransaction.location_id == loc.id)
                if start_dt:
                    range_query = range_query.filter(LocationTransaction.date >= start_dt)
                if end_dt:
                    range_query = range_query.filter(LocationTransaction.date <= end_dt)
                debit_val = Decimal(
                    str(
                        range_query.with_entities(
                            func.coalesce(func.sum(case((LocationTransaction.amount > 0, LocationTransaction.amount), else_=0)), 0)
                        ).scalar()
                        or 0
                    )
                )
                credit_val = Decimal(
                    str(
                        range_query.with_entities(
                            func.coalesce(func.sum(case((LocationTransaction.amount < 0, -LocationTransaction.amount), else_=0)), 0)
                        ).scalar()
                        or 0
                    )
                )
                closing_balance = opening_balance + debit_val - credit_val
            if balance_side == "debit" and closing_balance <= 0:
                continue
            if balance_side == "credit" and closing_balance >= 0:
                continue
            opening_sum += opening_balance
            debit_sum += debit_val
            credit_sum += credit_val
            closing_sum += closing_balance
            rows.append(
                {
                    "customer_id": str(loc.id),
                    "customer_name": loc.name,
                    "opening_balance": float(opening_balance),
                    "debit": float(debit_val),
                    "credit": float(credit_val),
                    "closing_balance": float(closing_balance),
                    "balance_desc": _balance_desc(closing_balance),
                }
            )
        totals = {
            "opening_sum": float(opening_sum),
            "debit_sum": float(debit_sum),
            "credit_sum": float(credit_sum),
            "closing_sum": float(closing_sum),
            "customers_count": len(rows),
        }
    elif report_key == "customer-statement":
        columns = [
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "warehouse_name", "label_ar": "المخزن"},
            {"key": "debit", "label_ar": "مدين"},
            {"key": "credit", "label_ar": "دائن"},
            {"key": "balance", "label_ar": "الرصيد"},
        ]
        if not customer_id:
            return JSONResponse({"error": "برجاء اختيار العميل."}, status_code=400)
        query = db.query(LocationTransaction).filter(LocationTransaction.location_id == int(customer_id))
        if start_dt:
            query = query.filter(LocationTransaction.date >= start_dt)
        if end_dt:
            query = query.filter(LocationTransaction.date <= end_dt)
        if invoice_type:
            query = query.filter(LocationTransaction.source_type == invoice_type)
        txs = query.order_by(LocationTransaction.date.asc(), LocationTransaction.id.asc()).all()
        transfer_ids = [t.source_id for t in txs if t.source_type in {"sale", "sale_return"} and t.source_id]
        transfer_map = {}
        if transfer_ids:
            transfers = (
                db.query(Transfer)
                .filter(Transfer.id.in_(transfer_ids))
                .all()
            )
            transfer_map = {t.id: t for t in transfers}
        opening_balance = Decimal("0")
        if start_dt:
            opening_balance = Decimal(
                str(
                    db.query(func.coalesce(func.sum(LocationTransaction.amount), 0))
                    .filter(LocationTransaction.location_id == int(customer_id))
                    .filter(LocationTransaction.date < start_dt)
                    .scalar()
                    or 0
                )
            )
        balance = opening_balance
        debit_sum = Decimal("0")
        credit_sum = Decimal("0")
        for tx in txs:
            amount = Decimal(str(tx.amount or 0))
            debit = amount if amount > 0 else Decimal("0")
            credit = abs(amount) if amount < 0 else Decimal("0")
            balance += amount
            debit_sum += debit
            credit_sum += credit
            warehouse_name = ""
            if tx.source_type in {"sale", "sale_return"}:
                transfer = transfer_map.get(tx.source_id)
                if transfer and transfer.from_location:
                    warehouse_name = transfer.from_location.name
            rows.append(
                {
                    "doc_no": str(tx.source_id or tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "description": tx.notes or "",
                    "warehouse_name": warehouse_name,
                    "debit": float(debit),
                    "credit": float(credit),
                    "balance": float(balance),
                }
            )
        totals = {
            "debit_sum": float(debit_sum),
            "credit_sum": float(credit_sum),
            "ending_balance": float(balance),
            "docs_count": len(rows),
        }
    elif report_key == "customers-statement":
        columns = [
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "debit", "label_ar": "مدين"},
            {"key": "credit", "label_ar": "دائن"},
            {"key": "balance", "label_ar": "الرصيد"},
        ]
        query = db.query(LocationTransaction, Location).join(Location, LocationTransaction.location_id == Location.id)
        query = query.filter(Location.type == "pharmacy")
        if customer_from_id and str(customer_from_id).isdigit():
            query = query.filter(LocationTransaction.location_id >= int(customer_from_id))
        if customer_to_id and str(customer_to_id).isdigit():
            query = query.filter(LocationTransaction.location_id <= int(customer_to_id))
        if start_dt:
            query = query.filter(LocationTransaction.date >= start_dt)
        if end_dt:
            query = query.filter(LocationTransaction.date <= end_dt)
        txs = query.order_by(LocationTransaction.location_id.asc(), LocationTransaction.date.asc(), LocationTransaction.id.asc()).all()
        balances = {}
        debit_sum = Decimal("0")
        credit_sum = Decimal("0")
        for tx, loc in txs:
            amount = Decimal(str(tx.amount or 0))
            debit = amount if amount > 0 else Decimal("0")
            credit = abs(amount) if amount < 0 else Decimal("0")
            prev = balances.get(loc.id, Decimal("0"))
            new_balance = prev + amount
            balances[loc.id] = new_balance
            debit_sum += debit
            credit_sum += credit
            rows.append(
                {
                    "customer_name": loc.name,
                    "doc_no": str(tx.source_id or tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "description": tx.notes or "",
                    "debit": float(debit),
                    "credit": float(credit),
                    "balance": float(new_balance),
                }
            )
        totals = {
            "debit_sum": float(debit_sum),
            "credit_sum": float(credit_sum),
            "ending_balance": float(sum(balances.values(), Decimal("0"))),
            "customers_count": len(balances),
            "docs_count": len(rows),
        }

    return JSONResponse({"title_ar": title_ar, "columns": columns, "rows": rows, "totals": totals, "sections": sections})


@app.post("/api/vendors/reports/{report_key}")
async def api_vendors_reports(report_key: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    report_titles = {
        "vendor-activity-volume": "حجم التعامل مع مورد",
        "vendors-balances": "أرصدة الموردين",
        "vendor-statement": "كشف حساب مورد",
        "vendors-statement": "كشف حساب الموردين",
    }
    if report_key not in report_titles:
        return JSONResponse({"error": "نوع تقرير غير صالح."}, status_code=400)

    title_ar = report_titles[report_key]
    columns = []
    rows = []
    totals = {}
    sections = {}

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    as_of_date = payload.get("as_of_date")
    vendor_id = payload.get("vendor_id")
    vendor_from_id = payload.get("vendor_from_id")
    vendor_to_id = payload.get("vendor_to_id")
    company_id = payload.get("company_id")
    warehouse_id = payload.get("warehouse_id")
    invoice_type = payload.get("invoice_type")
    sort_by = payload.get("sort_by") or "date"
    sort_dir = payload.get("sort_dir") or "asc"
    balance_side = payload.get("balance_side") or "all"
    search_text = payload.get("search_text")
    group_by = payload.get("group_by")

    start_dt = parse_date(date_from) if date_from else None
    end_dt = parse_date_end(date_to) if date_to else None
    as_of_dt = parse_date_end(as_of_date) if as_of_date else None

    def _balance_desc(value: Decimal) -> str:
        if value > 0:
            return "مدين"
        if value < 0:
            return "دائن"
        return "متوازن"

    if report_key == "vendor-activity-volume":
        columns = [
            {"key": "metric", "label_ar": "البند"},
            {"key": "value", "label_ar": "القيمة"},
        ]
        if not vendor_id:
            return JSONResponse({"error": "برجاء اختيار المورد."}, status_code=400)
        tx_query = db.query(SupplierTransaction).filter(SupplierTransaction.supplier_id == int(vendor_id))
        if start_dt:
            tx_query = tx_query.filter(SupplierTransaction.date >= start_dt)
        if end_dt:
            tx_query = tx_query.filter(SupplierTransaction.date <= end_dt)
        txs = tx_query.all()

        purchases_amount = sum((Decimal(str(t.amount or 0)) for t in txs if t.type == "purchase"), Decimal("0"))
        purchase_returns_amount = sum((abs(Decimal(str(t.amount or 0))) for t in txs if t.type == "purchase_return"), Decimal("0"))
        cash_payments_amount = sum((abs(Decimal(str(t.amount or 0))) for t in txs if t.type == "payment"), Decimal("0"))
        cash_receipts_amount = sum((Decimal(str(t.amount or 0)) for t in txs if t.type == "addition"), Decimal("0"))
        discounts_amount = sum((abs(Decimal(str(t.amount or 0))) for t in txs if t.type == "discount"), Decimal("0"))
        additions_amount = sum((Decimal(str(t.amount or 0)) for t in txs if t.type == "addition"), Decimal("0"))

        opening_balance = Decimal("0")
        if start_dt:
            opening_balance = get_supplier_balance(db, int(vendor_id), up_to=start_dt - timedelta(microseconds=1))
        ending_balance = (
            get_supplier_balance(db, int(vendor_id), up_to=end_dt)
            if end_dt
            else get_supplier_balance(db, int(vendor_id))
        )
        supplier = db.query(Supplier).filter(Supplier.id == int(vendor_id)).first()
        sections = {
            "analysis": {
                "purchases_amount": float(purchases_amount),
                "purchase_returns_amount": float(purchase_returns_amount),
                "cash_payments_amount": float(cash_payments_amount),
                "cash_receipts_amount": float(cash_receipts_amount),
                "discounts_amount": float(discounts_amount),
                "additions_amount": float(additions_amount),
                "opening_balance": float(opening_balance),
                "ending_balance": float(ending_balance),
            },
            "account_info": {
                "account_no": f"S-{supplier.id}" if supplier else "",
                "vendor_name": supplier.name if supplier else "",
                "status": "نشط",
                "current_balance": float(ending_balance),
                "balance_desc": _balance_desc(ending_balance),
            },
        }
        rows = [
            {"metric": "مشتريات", "value": float(purchases_amount)},
            {"metric": "مرتجعات مشتريات", "value": float(purchase_returns_amount)},
            {"metric": "مدفوعات نقدية", "value": float(cash_payments_amount)},
            {"metric": "تحصيلات نقدية", "value": float(cash_receipts_amount)},
            {"metric": "خصومات", "value": float(discounts_amount)},
            {"metric": "إضافات", "value": float(additions_amount)},
        ]

    elif report_key == "vendors-balances":
        columns = [
            {"key": "vendor_id", "label_ar": "كود المورد"},
            {"key": "vendor_name", "label_ar": "اسم المورد"},
            {"key": "opening_balance", "label_ar": "رصيد أول المدة"},
            {"key": "debit", "label_ar": "مدين"},
            {"key": "credit", "label_ar": "دائن"},
            {"key": "closing_balance", "label_ar": "رصيد آخر المدة"},
            {"key": "balance_desc", "label_ar": "وصف الرصيد"},
        ]
        supplier_query = db.query(Supplier)
        if vendor_from_id and str(vendor_from_id).isdigit():
            supplier_query = supplier_query.filter(Supplier.id >= int(vendor_from_id))
        if vendor_to_id and str(vendor_to_id).isdigit():
            supplier_query = supplier_query.filter(Supplier.id <= int(vendor_to_id))
        if search_text:
            like = f"%{search_text.strip()}%"
            supplier_query = supplier_query.filter(Supplier.name.ilike(like))
        suppliers = supplier_query.order_by(Supplier.id.asc()).all()

        opening_sum = Decimal("0")
        debit_sum = Decimal("0")
        credit_sum = Decimal("0")
        closing_sum = Decimal("0")
        for vendor in suppliers:
            if as_of_dt:
                opening_balance = Decimal("0")
                credit_val = Decimal(
                    str(
                        db.query(func.coalesce(func.sum(case((SupplierTransaction.amount > 0, SupplierTransaction.amount), else_=0)), 0))
                        .filter(SupplierTransaction.supplier_id == vendor.id)
                        .filter(SupplierTransaction.date <= as_of_dt)
                        .scalar()
                        or 0
                    )
                )
                debit_val = Decimal(
                    str(
                        db.query(func.coalesce(func.sum(case((SupplierTransaction.amount < 0, -SupplierTransaction.amount), else_=0)), 0))
                        .filter(SupplierTransaction.supplier_id == vendor.id)
                        .filter(SupplierTransaction.date <= as_of_dt)
                        .scalar()
                        or 0
                    )
                )
                closing_balance = get_supplier_balance(db, vendor.id, up_to=as_of_dt)
            else:
                opening_balance = Decimal("0")
                if start_dt:
                    opening_balance = get_supplier_balance(db, vendor.id, up_to=start_dt - timedelta(microseconds=1))
                range_query = db.query(SupplierTransaction).filter(SupplierTransaction.supplier_id == vendor.id)
                if start_dt:
                    range_query = range_query.filter(SupplierTransaction.date >= start_dt)
                if end_dt:
                    range_query = range_query.filter(SupplierTransaction.date <= end_dt)
                credit_val = Decimal(
                    str(
                        range_query.with_entities(
                            func.coalesce(func.sum(case((SupplierTransaction.amount > 0, SupplierTransaction.amount), else_=0)), 0)
                        ).scalar()
                        or 0
                    )
                )
                debit_val = Decimal(
                    str(
                        range_query.with_entities(
                            func.coalesce(func.sum(case((SupplierTransaction.amount < 0, -SupplierTransaction.amount), else_=0)), 0)
                        ).scalar()
                        or 0
                    )
                )
                closing_balance = opening_balance + credit_val - debit_val
            if balance_side == "debit" and closing_balance <= 0:
                continue
            if balance_side == "credit" and closing_balance >= 0:
                continue
            opening_sum += opening_balance
            debit_sum += debit_val
            credit_sum += credit_val
            closing_sum += closing_balance
            rows.append(
                {
                    "vendor_id": str(vendor.id),
                    "vendor_name": vendor.name,
                    "opening_balance": float(opening_balance),
                    "debit": float(debit_val),
                    "credit": float(credit_val),
                    "closing_balance": float(closing_balance),
                    "balance_desc": _balance_desc(closing_balance),
                }
            )
        totals = {
            "opening_sum": float(opening_sum),
            "debit_sum": float(debit_sum),
            "credit_sum": float(credit_sum),
            "closing_sum": float(closing_sum),
            "vendors_count": len(rows),
        }

    elif report_key == "vendor-statement":
        columns = [
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "warehouse_name", "label_ar": "المخزن"},
            {"key": "debit", "label_ar": "مدين"},
            {"key": "credit", "label_ar": "دائن"},
            {"key": "balance", "label_ar": "الرصيد"},
        ]
        if not vendor_id:
            return JSONResponse({"error": "برجاء اختيار المورد."}, status_code=400)
        query = db.query(SupplierTransaction).filter(SupplierTransaction.supplier_id == int(vendor_id))
        if start_dt:
            query = query.filter(SupplierTransaction.date >= start_dt)
        if end_dt:
            query = query.filter(SupplierTransaction.date <= end_dt)
        if invoice_type:
            query = query.filter(SupplierTransaction.type == invoice_type)
        txs = query.order_by(SupplierTransaction.date.asc(), SupplierTransaction.id.asc()).all()
        purchase_ids = [t.source_id for t in txs if t.source_type in {"purchase", "purchase_return"} and t.source_id]
        purchase_map = {}
        if purchase_ids:
            purchases = db.query(Purchase).filter(Purchase.id.in_(purchase_ids)).all()
            purchase_map = {p.id: p for p in purchases}
        opening_balance = Decimal("0")
        if start_dt:
            opening_balance = get_supplier_balance(db, int(vendor_id), up_to=start_dt - timedelta(microseconds=1))
        balance = opening_balance
        debit_sum = Decimal("0")
        credit_sum = Decimal("0")
        for tx in txs:
            amount = Decimal(str(tx.amount or 0))
            debit = abs(amount) if amount < 0 else Decimal("0")
            credit = amount if amount > 0 else Decimal("0")
            balance += amount
            debit_sum += debit
            credit_sum += credit
            warehouse_name = ""
            if tx.source_type in {"purchase", "purchase_return"}:
                purchase = purchase_map.get(tx.source_id)
                if purchase and purchase.location:
                    warehouse_name = purchase.location.name
            rows.append(
                {
                    "doc_no": str(tx.source_id or tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "description": tx.notes or "",
                    "warehouse_name": warehouse_name,
                    "debit": float(debit),
                    "credit": float(credit),
                    "balance": float(balance),
                }
            )
        totals = {
            "debit_sum": float(debit_sum),
            "credit_sum": float(credit_sum),
            "ending_balance": float(balance),
            "docs_count": len(rows),
        }

    elif report_key == "vendors-statement":
        columns = [
            {"key": "vendor_name", "label_ar": "المورد"},
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "debit", "label_ar": "مدين"},
            {"key": "credit", "label_ar": "دائن"},
            {"key": "balance", "label_ar": "الرصيد"},
        ]
        query = db.query(SupplierTransaction, Supplier).join(Supplier, SupplierTransaction.supplier_id == Supplier.id)
        if vendor_from_id and str(vendor_from_id).isdigit():
            query = query.filter(SupplierTransaction.supplier_id >= int(vendor_from_id))
        if vendor_to_id and str(vendor_to_id).isdigit():
            query = query.filter(SupplierTransaction.supplier_id <= int(vendor_to_id))
        if start_dt:
            query = query.filter(SupplierTransaction.date >= start_dt)
        if end_dt:
            query = query.filter(SupplierTransaction.date <= end_dt)
        txs = query.order_by(SupplierTransaction.supplier_id.asc(), SupplierTransaction.date.asc(), SupplierTransaction.id.asc()).all()
        balances = {}
        debit_sum = Decimal("0")
        credit_sum = Decimal("0")
        for tx, vendor in txs:
            if vendor.id not in balances:
                if start_dt:
                    balances[vendor.id] = get_supplier_balance(db, vendor.id, up_to=start_dt - timedelta(microseconds=1))
                else:
                    balances[vendor.id] = Decimal("0")
            amount = Decimal(str(tx.amount or 0))
            debit = abs(amount) if amount < 0 else Decimal("0")
            credit = amount if amount > 0 else Decimal("0")
            balances[vendor.id] = balances[vendor.id] + amount
            debit_sum += debit
            credit_sum += credit
            rows.append(
                {
                    "vendor_name": vendor.name,
                    "doc_no": str(tx.source_id or tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "description": tx.notes or "",
                    "debit": float(debit),
                    "credit": float(credit),
                    "balance": float(balances[vendor.id]),
                }
            )
        totals = {
            "debit_sum": float(debit_sum),
            "credit_sum": float(credit_sum),
            "ending_balance": float(sum(balances.values(), Decimal("0"))),
            "vendors_count": len(balances),
            "docs_count": len(rows),
        }

    return JSONResponse({"title_ar": title_ar, "columns": columns, "rows": rows, "totals": totals, "sections": sections})


@app.post("/api/purchases/reports/{report_key}")
async def api_purchases_reports(report_key: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    report_titles = {
        "purchase-orders-list": "كشف أوامر الشراء",
        "incoming-items-qty": "تقرير عن كميات الأصناف الواردة",
        "item-purchases-volume": "حجم مشتريات أصناف",
    }
    if report_key not in report_titles:
        return JSONResponse({"error": "نوع تقرير غير صالح."}, status_code=400)

    title_ar = report_titles[report_key]
    columns = []
    rows = []
    totals = {}

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    supplier_id = payload.get("supplier_id")
    warehouse_id = payload.get("warehouse_id")
    item_id = payload.get("item_id")
    category_id = payload.get("category_id")
    doc_no = payload.get("doc_no")
    order_status = payload.get("order_status")
    sort_by = payload.get("sort_by") or "date"
    sort_dir = payload.get("sort_dir") or "asc"

    start_dt = parse_date(date_from) if date_from else None
    end_dt = parse_date_end(date_to) if date_to else None

    def _order_status_label(value: Optional[str]) -> str:
        if value == "open":
            return "مفتوح"
        if value == "converted":
            return "مغلق"
        if value == "cancelled":
            return "ملغي"
        return value or ""

    if report_key == "purchase-orders-list":
        columns = [
            {"key": "po_no", "label_ar": "رقم الأمر"},
            {"key": "po_date", "label_ar": "التاريخ"},
            {"key": "supplier_name", "label_ar": "المورد"},
            {"key": "warehouse_name", "label_ar": "المخزن"},
            {"key": "status", "label_ar": "الحالة"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
            {"key": "notes", "label_ar": "ملاحظات"},
        ]
        query = db.query(PurchaseOrder)
        if start_dt:
            query = query.filter(PurchaseOrder.date >= start_dt)
        if end_dt:
            query = query.filter(PurchaseOrder.date <= end_dt)
        if supplier_id and str(supplier_id).isdigit():
            query = query.filter(PurchaseOrder.supplier_id == int(supplier_id))
        if warehouse_id and str(warehouse_id).isdigit():
            query = query.filter(PurchaseOrder.location_id == int(warehouse_id))
        if order_status:
            query = query.filter(PurchaseOrder.status == order_status)
        if doc_no:
            if str(doc_no).isdigit():
                query = query.filter(PurchaseOrder.id == int(doc_no))
        if sort_by == "doc_no":
            query = query.order_by(PurchaseOrder.id.desc() if sort_dir == "desc" else PurchaseOrder.id.asc())
        else:
            query = query.order_by(PurchaseOrder.date.desc() if sort_dir == "desc" else PurchaseOrder.date.asc())

        total_sum = Decimal("0")
        for order in query.all():
            total_sum += Decimal(str(order.total or 0))
            rows.append(
                {
                    "po_no": f"PO-{order.id}",
                    "po_date": order.date.strftime("%Y-%m-%d") if order.date else "",
                    "supplier_name": order.supplier.name if order.supplier else "",
                    "warehouse_name": order.location.name if order.location else "",
                    "status": _order_status_label(order.status),
                    "total_amount": float(order.total or 0),
                    "notes": order.notes or "",
                }
            )
        totals = {
            "total_sum": float(total_sum),
            "orders_count": len(rows),
        }

    elif report_key == "incoming-items-qty":
        columns = [
            {"key": "doc_no", "label_ar": "المستند"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "supplier_name", "label_ar": "المورد"},
            {"key": "warehouse_name", "label_ar": "المخزن"},
            {"key": "item_code", "label_ar": "كود الصنف"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty_in", "label_ar": "الكمية الواردة"},
            {"key": "unit_cost", "label_ar": "تكلفة الوحدة"},
            {"key": "total_cost", "label_ar": "إجمالي التكلفة"},
        ]
        query = (
            db.query(PurchaseItem, Purchase, Supplier, Location, Item)
            .join(Purchase, PurchaseItem.purchase_id == Purchase.id)
            .join(Supplier, Purchase.supplier_id == Supplier.id)
            .join(Location, Purchase.location_id == Location.id)
            .join(Item, PurchaseItem.item_id == Item.id)
            .filter(Purchase.kind == "purchase")
        )
        if start_dt:
            query = query.filter(Purchase.date >= start_dt)
        if end_dt:
            query = query.filter(Purchase.date <= end_dt)
        if supplier_id and str(supplier_id).isdigit():
            query = query.filter(Purchase.supplier_id == int(supplier_id))
        if warehouse_id and str(warehouse_id).isdigit():
            query = query.filter(Purchase.location_id == int(warehouse_id))
        if item_id and str(item_id).isdigit():
            query = query.filter(PurchaseItem.item_id == int(item_id))
        if doc_no:
            if str(doc_no).isdigit():
                query = query.filter(Purchase.id == int(doc_no))
            else:
                query = query.filter(Purchase.invoice_no.ilike(f"%{str(doc_no).strip()}%"))
        if sort_by == "doc_no":
            query = query.order_by(Purchase.id.desc() if sort_dir == "desc" else Purchase.id.asc())
        else:
            query = query.order_by(Purchase.date.desc() if sort_dir == "desc" else Purchase.date.asc())

        qty_sum = Decimal("0")
        total_cost_sum = Decimal("0")
        doc_ids = set()
        for line, purchase, supplier, warehouse, item in query.all():
            qty_in = Decimal(str(line.qty or 0)) + Decimal(str(line.bonus_qty or 0))
            total_cost = Decimal(str(line.line_total or 0))
            qty_sum += qty_in
            total_cost_sum += total_cost
            doc_ids.add(purchase.id)
            rows.append(
                {
                    "doc_no": str(purchase.id),
                    "doc_date": purchase.date.strftime("%Y-%m-%d") if purchase.date else "",
                    "supplier_name": supplier.name if supplier else "",
                    "warehouse_name": warehouse.name if warehouse else "",
                    "item_code": str(item.id),
                    "item_name": item.name,
                    "qty_in": float(qty_in),
                    "unit_cost": float(line.unit_price or 0),
                    "total_cost": float(total_cost),
                }
            )
        totals = {
            "qty_sum": float(qty_sum),
            "total_cost_sum": float(total_cost_sum),
            "docs_count": len(doc_ids),
        }

    elif report_key == "item-purchases-volume":
        columns = [
            {"key": "item_code", "label_ar": "كود الصنف"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty_purchased", "label_ar": "الكمية"},
            {"key": "avg_unit_cost", "label_ar": "متوسط التكلفة"},
            {"key": "total_cost", "label_ar": "إجمالي التكلفة"},
            {"key": "last_purchase_date", "label_ar": "آخر شراء"},
        ]
        query = (
            db.query(
                Item.id.label("item_id"),
                Item.name.label("item_name"),
                func.coalesce(func.sum(PurchaseItem.qty + PurchaseItem.bonus_qty), 0).label("qty_sum"),
                func.coalesce(func.avg(PurchaseItem.unit_price), 0).label("avg_cost"),
                func.coalesce(func.sum(PurchaseItem.line_total), 0).label("total_cost"),
                func.max(Purchase.date).label("last_date"),
            )
            .join(PurchaseItem, PurchaseItem.item_id == Item.id)
            .join(Purchase, PurchaseItem.purchase_id == Purchase.id)
            .filter(Purchase.kind == "purchase")
        )
        if start_dt:
            query = query.filter(Purchase.date >= start_dt)
        if end_dt:
            query = query.filter(Purchase.date <= end_dt)
        if supplier_id and str(supplier_id).isdigit():
            query = query.filter(Purchase.supplier_id == int(supplier_id))
        if warehouse_id and str(warehouse_id).isdigit():
            query = query.filter(Purchase.location_id == int(warehouse_id))
        if category_id and str(category_id).isdigit():
            query = query.filter(Item.category_id == int(category_id))

        query = query.group_by(Item.id, Item.name)
        if sort_by == "doc_no":
            query = query.order_by(Item.id.desc() if sort_dir == "desc" else Item.id.asc())
        else:
            query = query.order_by(Item.name.desc() if sort_dir == "desc" else Item.name.asc())

        qty_sum = Decimal("0")
        total_cost_sum = Decimal("0")
        for row in query.all():
            qty_sum += Decimal(str(row.qty_sum or 0))
            total_cost_sum += Decimal(str(row.total_cost or 0))
            rows.append(
                {
                    "item_code": str(row.item_id),
                    "item_name": row.item_name,
                    "qty_purchased": float(row.qty_sum or 0),
                    "avg_unit_cost": float(row.avg_cost or 0),
                    "total_cost": float(row.total_cost or 0),
                    "last_purchase_date": row.last_date.strftime("%Y-%m-%d") if row.last_date else "",
                }
            )
        totals = {
            "qty_sum": float(qty_sum),
            "total_cost_sum": float(total_cost_sum),
            "items_count": len(rows),
        }

    return JSONResponse({"title_ar": title_ar, "columns": columns, "rows": rows, "totals": totals})


def _get_system_start_date_for_doctors(db: Session) -> date:
    candidates = []
    min_transfer = db.query(func.min(Transfer.date)).scalar()
    min_doctor_tx = db.query(func.min(DoctorTransaction.date)).scalar()
    for val in (min_transfer, min_doctor_tx):
        if not val:
            continue
        if isinstance(val, datetime):
            candidates.append(val.date())
        else:
            candidates.append(val)
    return min(candidates) if candidates else date.today()


@app.get("/api/lookups/system-period")
async def api_lookup_system_period(db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    start_date = _get_system_start_date_for_doctors(db)
    end_date = date.today()
    return {
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
    }


@app.post("/api/doctors/reports/{report_key}")
async def api_doctors_reports(report_key: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    report_titles = {
        "doctor-pharmacy-dispense": "حركة نزول الدواء للصيدلية",
        "doctor-commission-by-pharmacy": "عمولة الطبيب حسب الصيدلية",
        "doctor-commission-by-item": "عمولة الطبيب حسب الدواء",
        "doctor-statement": "كشف حساب الطبيب",
        "doctor-payments": "مدفوعات الطبيب",
    }
    if report_key not in report_titles:
        return JSONResponse({"error": "نوع تقرير غير صالح."}, status_code=400)

    title_ar = report_titles[report_key]
    columns = []
    rows = []
    totals = {}

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    doctor_id = payload.get("doctor_id")
    pharmacy_id = payload.get("pharmacy_id")
    item_id = payload.get("item_id")
    doc_no = payload.get("doc_no")
    sort_by = payload.get("sort_by") or "date"
    sort_dir = payload.get("sort_dir") or "asc"
    include_opening = payload.get("include_opening_balance", True)
    opening_as_of = payload.get("opening_balance_as_of")

    if not date_from or not date_to:
        default_start = _get_system_start_date_for_doctors(db)
        default_end = date.today()
        date_from = date_from or default_start.strftime("%Y-%m-%d")
        date_to = date_to or default_end.strftime("%Y-%m-%d")

    start_dt = parse_date(date_from) if date_from else None
    end_dt = parse_date_end(date_to) if date_to else None

    def _apply_sort(query, date_col, id_col):
        if sort_by == "doc_no":
            return query.order_by(id_col.desc() if sort_dir == "desc" else id_col.asc())
        return query.order_by(date_col.desc() if sort_dir == "desc" else date_col.asc())

    def _commission_rate(line_total: Decimal, commission_amount: Decimal) -> Decimal:
        if line_total <= 0:
            return Decimal("0")
        return (commission_amount / line_total) * Decimal("100")

    if report_key == "doctor-pharmacy-dispense":
        columns = [
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "pharmacy_name", "label_ar": "الصيدلية"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty", "label_ar": "الكمية"},
            {"key": "unit_price", "label_ar": "السعر"},
            {"key": "line_total", "label_ar": "إجمالي السطر"},
            {"key": "commission_rate", "label_ar": "نسبة العمولة"},
            {"key": "commission_amount", "label_ar": "العمولة"},
        ]
        query = (
            db.query(TransferLine, Transfer, Location, Item)
            .join(Transfer, TransferLine.transfer_id == Transfer.id)
            .join(Location, Transfer.to_location_id == Location.id)
            .join(Item, TransferLine.item_id == Item.id)
            .filter(TransferLine.doctor_id.isnot(None))
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if doctor_id and str(doctor_id).isdigit():
            query = query.filter(TransferLine.doctor_id == int(doctor_id))
        if pharmacy_id and str(pharmacy_id).isdigit():
            query = query.filter(Transfer.to_location_id == int(pharmacy_id))
        if item_id and str(item_id).isdigit():
            query = query.filter(TransferLine.item_id == int(item_id))
        if doc_no and str(doc_no).isdigit():
            query = query.filter(Transfer.id == int(doc_no))
        query = _apply_sort(query, Transfer.date, Transfer.id)

        qty_sum = Decimal("0")
        line_total_sum = Decimal("0")
        commission_sum = Decimal("0")
        for line, sale, pharmacy, item in query.all():
            qty = Decimal(str(line.requested_qty or 0))
            line_total = Decimal(str(line.line_total or 0))
            commission_amount = Decimal(str(line.commission_amount or 0))
            rate = _commission_rate(line_total, commission_amount)
            qty_sum += qty
            line_total_sum += line_total
            commission_sum += commission_amount
            rows.append(
                {
                    "doc_no": str(sale.id),
                    "doc_date": sale.date.strftime("%Y-%m-%d") if sale.date else "",
                    "pharmacy_name": pharmacy.name if pharmacy else "",
                    "item_name": item.name,
                    "qty": float(qty),
                    "unit_price": float(line.unit_price or 0),
                    "line_total": float(line_total),
                    "commission_rate": float(rate),
                    "commission_amount": float(commission_amount),
                }
            )
        totals = {
            "qty_sum": float(qty_sum),
            "line_total_sum": float(line_total_sum),
            "commission_sum": float(commission_sum),
        }

    elif report_key == "doctor-commission-by-pharmacy":
        columns = [
            {"key": "pharmacy_name", "label_ar": "الصيدلية"},
            {"key": "qty_sum", "label_ar": "الكمية"},
            {"key": "sales_sum", "label_ar": "إجمالي المبيعات"},
            {"key": "commission_rate_avg", "label_ar": "متوسط العمولة"},
            {"key": "commission_sum", "label_ar": "إجمالي العمولة"},
        ]
        query = (
            db.query(
                Location.name.label("pharmacy_name"),
                func.coalesce(func.sum(TransferLine.requested_qty), 0).label("qty_sum"),
                func.coalesce(func.sum(TransferLine.line_total), 0).label("sales_sum"),
                func.coalesce(func.sum(TransferLine.commission_amount), 0).label("commission_sum"),
            )
            .join(Transfer, TransferLine.transfer_id == Transfer.id)
            .join(Location, Transfer.to_location_id == Location.id)
            .filter(TransferLine.doctor_id.isnot(None))
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if doctor_id and str(doctor_id).isdigit():
            query = query.filter(TransferLine.doctor_id == int(doctor_id))
        if pharmacy_id and str(pharmacy_id).isdigit():
            query = query.filter(Transfer.to_location_id == int(pharmacy_id))
        query = query.group_by(Location.name)
        query = query.order_by(Location.name.desc() if sort_dir == "desc" else Location.name.asc())

        sales_sum_total = Decimal("0")
        commission_sum_total = Decimal("0")
        for row in query.all():
            sales_sum = Decimal(str(row.sales_sum or 0))
            commission_sum = Decimal(str(row.commission_sum or 0))
            rate_avg = _commission_rate(sales_sum, commission_sum)
            sales_sum_total += sales_sum
            commission_sum_total += commission_sum
            rows.append(
                {
                    "pharmacy_name": row.pharmacy_name,
                    "qty_sum": float(row.qty_sum or 0),
                    "sales_sum": float(sales_sum),
                    "commission_rate_avg": float(rate_avg),
                    "commission_sum": float(commission_sum),
                }
            )
        totals = {
            "sales_sum": float(sales_sum_total),
            "commission_sum": float(commission_sum_total),
        }

    elif report_key == "doctor-commission-by-item":
        columns = [
            {"key": "item_code", "label_ar": "كود الصنف"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty_sum", "label_ar": "الكمية"},
            {"key": "sales_sum", "label_ar": "إجمالي المبيعات"},
            {"key": "commission_rate_avg", "label_ar": "متوسط العمولة"},
            {"key": "commission_sum", "label_ar": "إجمالي العمولة"},
        ]
        query = (
            db.query(
                Item.id.label("item_id"),
                Item.name.label("item_name"),
                func.coalesce(func.sum(TransferLine.requested_qty), 0).label("qty_sum"),
                func.coalesce(func.sum(TransferLine.line_total), 0).label("sales_sum"),
                func.coalesce(func.sum(TransferLine.commission_amount), 0).label("commission_sum"),
            )
            .join(Transfer, TransferLine.transfer_id == Transfer.id)
            .join(Item, TransferLine.item_id == Item.id)
            .filter(TransferLine.doctor_id.isnot(None))
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if doctor_id and str(doctor_id).isdigit():
            query = query.filter(TransferLine.doctor_id == int(doctor_id))
        if item_id and str(item_id).isdigit():
            query = query.filter(TransferLine.item_id == int(item_id))
        query = query.group_by(Item.id, Item.name)
        query = query.order_by(Item.name.desc() if sort_dir == "desc" else Item.name.asc())

        qty_sum_total = Decimal("0")
        sales_sum_total = Decimal("0")
        commission_sum_total = Decimal("0")
        for row in query.all():
            sales_sum = Decimal(str(row.sales_sum or 0))
            commission_sum = Decimal(str(row.commission_sum or 0))
            rate_avg = _commission_rate(sales_sum, commission_sum)
            qty_sum_total += Decimal(str(row.qty_sum or 0))
            sales_sum_total += sales_sum
            commission_sum_total += commission_sum
            rows.append(
                {
                    "item_code": str(row.item_id),
                    "item_name": row.item_name,
                    "qty_sum": float(row.qty_sum or 0),
                    "sales_sum": float(sales_sum),
                    "commission_rate_avg": float(rate_avg),
                    "commission_sum": float(commission_sum),
                }
            )
        totals = {
            "qty_sum": float(qty_sum_total),
            "sales_sum": float(sales_sum_total),
            "commission_sum": float(commission_sum_total),
        }

    elif report_key == "doctor-statement":
        columns = [
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "txn_type", "label_ar": "نوع الحركة"},
            {"key": "description", "label_ar": "البيان"},
            {"key": "pharmacy_name", "label_ar": "الصيدلية"},
            {"key": "debit", "label_ar": "مدين"},
            {"key": "credit", "label_ar": "دائن"},
            {"key": "balance", "label_ar": "الرصيد"},
        ]
        if not doctor_id:
            return JSONResponse({"error": "برجاء اختيار الطبيب."}, status_code=400)
        query = db.query(DoctorTransaction).filter(DoctorTransaction.doctor_id == int(doctor_id))
        if start_dt:
            query = query.filter(DoctorTransaction.date >= start_dt)
        if end_dt:
            query = query.filter(DoctorTransaction.date <= end_dt)
        if pharmacy_id and str(pharmacy_id).isdigit():
            query = query.filter(DoctorTransaction.pharmacy_location_id == int(pharmacy_id))
        query = query.order_by(DoctorTransaction.date.asc(), DoctorTransaction.id.asc())

        opening_balance = Decimal("0")
        if include_opening:
            if opening_as_of:
                opening_date = parse_date(opening_as_of)
                opening_balance = Decimal(
                    str(
                        db.query(func.coalesce(func.sum(DoctorTransaction.amount), 0))
                        .filter(DoctorTransaction.doctor_id == int(doctor_id))
                        .filter(DoctorTransaction.date < opening_date)
                        .scalar()
                        or 0
                    )
                )
            elif start_dt:
                opening_balance = Decimal(
                    str(
                        db.query(func.coalesce(func.sum(DoctorTransaction.amount), 0))
                        .filter(DoctorTransaction.doctor_id == int(doctor_id))
                        .filter(DoctorTransaction.date < start_dt)
                        .scalar()
                        or 0
                    )
                )

        balance = opening_balance
        debit_sum = Decimal("0")
        credit_sum = Decimal("0")
        docs_count = 0
        if include_opening:
            rows.append(
                {
                    "doc_no": "-",
                    "doc_date": date_from or "",
                    "txn_type": "opening",
                    "description": "رصيد أول المدة",
                    "pharmacy_name": "",
                    "debit": 0.0,
                    "credit": float(opening_balance),
                    "balance": float(balance),
                }
            )
            docs_count += 1

        for tx in query.all():
            amount = Decimal(str(tx.amount or 0))
            txn_type = tx.type or ""
            description = tx.notes or ""
            pharmacy_name = ""
            if tx.pharmacy_location_id:
                loc = db.query(Location).filter(Location.id == tx.pharmacy_location_id).first()
                pharmacy_name = loc.name if loc else ""

            if txn_type == "payment":
                debit = abs(amount)
                credit = Decimal("0")
                balance -= debit
                debit_sum += debit
            else:
                debit = Decimal("0")
                credit = amount
                balance += credit
                credit_sum += credit

            rows.append(
                {
                    "doc_no": str(tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "txn_type": txn_type,
                    "description": description,
                    "pharmacy_name": pharmacy_name,
                    "debit": float(debit),
                    "credit": float(credit),
                    "balance": float(balance),
                }
            )
            docs_count += 1

        totals = {
            "debit_sum": float(debit_sum),
            "credit_sum": float(credit_sum),
            "ending_balance": float(balance),
            "docs_count": docs_count,
        }

    elif report_key == "doctor-payments":
        columns = [
            {"key": "doc_no", "label_ar": "رقم المستند"},
            {"key": "doc_date", "label_ar": "التاريخ"},
            {"key": "doctor_name", "label_ar": "الطبيب"},
            {"key": "amount", "label_ar": "المبلغ"},
            {"key": "notes", "label_ar": "البيان"},
        ]
        query = db.query(DoctorTransaction, Doctor).join(Doctor, DoctorTransaction.doctor_id == Doctor.id)
        query = query.filter(DoctorTransaction.type == "payment")
        if doctor_id and str(doctor_id).isdigit():
            query = query.filter(DoctorTransaction.doctor_id == int(doctor_id))
        if start_dt:
            query = query.filter(DoctorTransaction.date >= start_dt)
        if end_dt:
            query = query.filter(DoctorTransaction.date <= end_dt)
        if doc_no and str(doc_no).isdigit():
            query = query.filter(DoctorTransaction.id == int(doc_no))
        query = _apply_sort(query, DoctorTransaction.date, DoctorTransaction.id)

        amount_sum = Decimal("0")
        for tx, doctor in query.all():
            amount = Decimal(str(tx.amount or 0))
            amount_sum += abs(amount)
            rows.append(
                {
                    "doc_no": str(tx.id),
                    "doc_date": tx.date.strftime("%Y-%m-%d") if tx.date else "",
                    "doctor_name": doctor.name if doctor else "",
                    "amount": float(abs(amount)),
                    "notes": tx.notes or "",
                }
            )
        totals = {
            "amount_sum": float(amount_sum),
            "docs_count": len(rows),
        }

    return JSONResponse({"title_ar": title_ar, "columns": columns, "rows": rows, "totals": totals})


@app.post("/api/sales/reports/{report_key}")
async def api_sales_reports(report_key: str, request: Request, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    payload = await request.json()
    report_titles = {
        "sales-invoices-list": "كشف فواتير المبيعات",
        "sales-returns-list": "كشف مرتجعات المبيعات",
        "item-sales-report": "تقرير مبيعات أصناف",
        "item-sales-volume": "حجم مبيعات أصناف",
        "customer-sales-summary": "مبيعات العملاء",
        "rep-sales-summary": "مبيعات مندوب",
        "sales-profit-report": "أرباح المبيعات",
    }
    if report_key not in report_titles:
        return JSONResponse({"error": "نوع تقرير غير صالح."}, status_code=400)

    title_ar = report_titles[report_key]
    columns = []
    rows = []
    totals = {}

    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    customer_id = payload.get("customer_id")
    rep_id = payload.get("rep_id")
    warehouse_id = payload.get("warehouse_id")
    item_id = payload.get("item_id")
    category_id = payload.get("category_id")
    invoice_type = payload.get("invoice_type")
    doc_no = payload.get("doc_no")
    sort_by = payload.get("sort_by") or "date"
    sort_dir = payload.get("sort_dir") or "asc"

    start_dt = parse_date(date_from) if date_from else None
    end_dt = parse_date_end(date_to) if date_to else None

    def _apply_sort(query, date_col, id_col):
        if sort_by == "doc_no":
            return query.order_by(id_col.desc() if sort_dir == "desc" else id_col.asc())
        return query.order_by(date_col.desc() if sort_dir == "desc" else date_col.asc())

    paid_rows = (
        db.query(SalesInvoicePayment.sale_id, func.coalesce(func.sum(SalesInvoicePayment.amount), 0))
        .group_by(SalesInvoicePayment.sale_id)
        .all()
    )
    paid_map = {row[0]: Decimal(str(row[1] or 0)) for row in paid_rows}

    if report_key == "sales-invoices-list":
        columns = [
            {"key": "invoice_no", "label_ar": "رقم الفاتورة"},
            {"key": "invoice_date", "label_ar": "التاريخ"},
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "rep_name", "label_ar": "المندوب"},
            {"key": "warehouse_name", "label_ar": "المخزن"},
            {"key": "net_amount", "label_ar": "الصافي"},
            {"key": "tax_amount", "label_ar": "الضريبة"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
            {"key": "payment_type", "label_ar": "طريقة السداد"},
        ]
        query = db.query(Transfer).filter(Transfer.kind == "sale")
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if customer_id and str(customer_id).isdigit():
            query = query.filter(Transfer.to_location_id == int(customer_id))
        if rep_id and str(rep_id).isdigit():
            query = query.filter(Transfer.rep_id == int(rep_id))
        if warehouse_id and str(warehouse_id).isdigit():
            query = query.filter(Transfer.from_location_id == int(warehouse_id))
        if doc_no and str(doc_no).isdigit():
            query = query.filter(Transfer.id == int(doc_no))
        query = _apply_sort(query, Transfer.date, Transfer.id)

        net_sum = Decimal("0")
        total_sum = Decimal("0")
        for sale in query.all():
            total = Decimal(str(sale.total or 0))
            paid_total = paid_map.get(sale.id, Decimal("0"))
            if invoice_type == "cash" and paid_total < total:
                continue
            if invoice_type == "credit" and paid_total >= total:
                continue
            net_sum += total
            total_sum += total
            rows.append(
                {
                    "invoice_no": f"INV-{sale.id}",
                    "invoice_date": sale.date.strftime("%Y-%m-%d") if sale.date else "",
                    "customer_name": sale.to_location.name if sale.to_location else "",
                    "rep_name": sale.rep.name if sale.rep else "",
                    "warehouse_name": sale.from_location.name if sale.from_location else "",
                    "net_amount": float(total),
                    "tax_amount": 0.0,
                    "total_amount": float(total),
                    "payment_type": "نقدي" if paid_total >= total else "آجل",
                }
            )
        totals = {
            "net_sum": float(net_sum),
            "total_sum": float(total_sum),
            "invoices_count": len(rows),
        }

    elif report_key == "sales-returns-list":
        columns = [
            {"key": "return_no", "label_ar": "رقم المرتجع"},
            {"key": "return_date", "label_ar": "التاريخ"},
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "rep_name", "label_ar": "المندوب"},
            {"key": "net_amount", "label_ar": "الصافي"},
            {"key": "tax_amount", "label_ar": "الضريبة"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
        ]
        query = db.query(Transfer).filter(Transfer.kind == "sale_return")
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if customer_id and str(customer_id).isdigit():
            query = query.filter(Transfer.from_location_id == int(customer_id))
        if rep_id and str(rep_id).isdigit():
            query = query.filter(Transfer.rep_id == int(rep_id))
        if doc_no and str(doc_no).isdigit():
            query = query.filter(Transfer.id == int(doc_no))
        query = _apply_sort(query, Transfer.date, Transfer.id)

        net_sum = Decimal("0")
        total_sum = Decimal("0")
        for ret in query.all():
            total = Decimal(str(ret.total or 0))
            net_sum += total
            total_sum += total
            rows.append(
                {
                    "return_no": f"RET-{ret.id}",
                    "return_date": ret.date.strftime("%Y-%m-%d") if ret.date else "",
                    "customer_name": ret.from_location.name if ret.from_location else "",
                    "rep_name": ret.rep.name if ret.rep else "",
                    "net_amount": float(total),
                    "tax_amount": 0.0,
                    "total_amount": float(total),
                }
            )
        totals = {
            "net_sum": float(net_sum),
            "total_sum": float(total_sum),
            "returns_count": len(rows),
        }

    elif report_key == "item-sales-report":
        columns = [
            {"key": "invoice_no", "label_ar": "رقم الفاتورة"},
            {"key": "invoice_date", "label_ar": "التاريخ"},
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "item_code", "label_ar": "كود الصنف"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty", "label_ar": "الكمية"},
            {"key": "unit_price", "label_ar": "سعر البيع"},
            {"key": "discount", "label_ar": "خصم %"},
            {"key": "net_amount", "label_ar": "الصافي"},
            {"key": "total_amount", "label_ar": "الإجمالي"},
        ]
        query = (
            db.query(TransferLine, Transfer, Location, Item)
            .join(Transfer, TransferLine.transfer_id == Transfer.id)
            .join(Location, Transfer.to_location_id == Location.id)
            .join(Item, TransferLine.item_id == Item.id)
            .filter(Transfer.kind == "sale")
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if customer_id and str(customer_id).isdigit():
            query = query.filter(Transfer.to_location_id == int(customer_id))
        if warehouse_id and str(warehouse_id).isdigit():
            query = query.filter(Transfer.from_location_id == int(warehouse_id))
        if item_id and str(item_id).isdigit():
            query = query.filter(TransferLine.item_id == int(item_id))
        if doc_no and str(doc_no).isdigit():
            query = query.filter(Transfer.id == int(doc_no))
        query = _apply_sort(query, Transfer.date, Transfer.id)

        qty_sum = Decimal("0")
        net_sum = Decimal("0")
        total_sum = Decimal("0")
        for line, sale, customer, item in query.all():
            qty = Decimal(str(line.requested_qty or 0))
            line_total = Decimal(str(line.line_total or 0))
            qty_sum += qty
            net_sum += line_total
            total_sum += line_total
            rows.append(
                {
                    "invoice_no": f"INV-{sale.id}",
                    "invoice_date": sale.date.strftime("%Y-%m-%d") if sale.date else "",
                    "customer_name": customer.name if customer else "",
                    "item_code": str(item.id),
                    "item_name": item.name,
                    "qty": float(qty),
                    "unit_price": float(line.unit_price or 0),
                    "discount": float(line.discount_amount or 0),
                    "net_amount": float(line_total),
                    "total_amount": float(line_total),
                }
            )
        totals = {
            "qty_sum": float(qty_sum),
            "net_sum": float(net_sum),
            "total_sum": float(total_sum),
        }

    elif report_key == "item-sales-volume":
        columns = [
            {"key": "item_code", "label_ar": "كود الصنف"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty_sold", "label_ar": "الكمية"},
            {"key": "avg_unit_price", "label_ar": "متوسط السعر"},
            {"key": "total_sales", "label_ar": "إجمالي المبيعات"},
            {"key": "last_sale_date", "label_ar": "آخر بيع"},
        ]
        query = (
            db.query(
                Item.id.label("item_id"),
                Item.name.label("item_name"),
                func.coalesce(func.sum(TransferLine.requested_qty), 0).label("qty_sum"),
                func.coalesce(func.avg(TransferLine.unit_price), 0).label("avg_price"),
                func.coalesce(func.sum(TransferLine.line_total), 0).label("total_sales"),
                func.max(Transfer.date).label("last_date"),
            )
            .join(TransferLine, TransferLine.item_id == Item.id)
            .join(Transfer, TransferLine.transfer_id == Transfer.id)
            .filter(Transfer.kind == "sale")
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if warehouse_id and str(warehouse_id).isdigit():
            query = query.filter(Transfer.from_location_id == int(warehouse_id))
        if category_id and str(category_id).isdigit():
            query = query.filter(Item.category_id == int(category_id))
        query = query.group_by(Item.id, Item.name)
        query = query.order_by(Item.name.desc() if sort_dir == "desc" else Item.name.asc())

        qty_sum = Decimal("0")
        total_sales_sum = Decimal("0")
        for row in query.all():
            qty_sum += Decimal(str(row.qty_sum or 0))
            total_sales_sum += Decimal(str(row.total_sales or 0))
            rows.append(
                {
                    "item_code": str(row.item_id),
                    "item_name": row.item_name,
                    "qty_sold": float(row.qty_sum or 0),
                    "avg_unit_price": float(row.avg_price or 0),
                    "total_sales": float(row.total_sales or 0),
                    "last_sale_date": row.last_date.strftime("%Y-%m-%d") if row.last_date else "",
                }
            )
        totals = {
            "qty_sum": float(qty_sum),
            "total_sales_sum": float(total_sales_sum),
            "items_count": len(rows),
        }

    elif report_key == "customer-sales-summary":
        columns = [
            {"key": "customer_name", "label_ar": "العميل"},
            {"key": "invoices_count", "label_ar": "عدد الفواتير"},
            {"key": "qty_sum", "label_ar": "إجمالي الكمية"},
            {"key": "net_sales", "label_ar": "الصافي"},
            {"key": "total_sales", "label_ar": "إجمالي المبيعات"},
        ]
        query = (
            db.query(
                Location.id.label("customer_id"),
                Location.name.label("customer_name"),
                func.count(func.distinct(Transfer.id)).label("invoices_count"),
                func.coalesce(func.sum(TransferLine.requested_qty), 0).label("qty_sum"),
                func.coalesce(func.sum(TransferLine.line_total), 0).label("total_sales"),
            )
            .join(Transfer, Transfer.to_location_id == Location.id)
            .join(TransferLine, TransferLine.transfer_id == Transfer.id)
            .filter(Transfer.kind == "sale")
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if customer_id and str(customer_id).isdigit():
            query = query.filter(Location.id == int(customer_id))
        query = query.group_by(Location.id, Location.name)
        query = query.order_by(Location.name.desc() if sort_dir == "desc" else Location.name.asc())

        total_sales_sum = Decimal("0")
        for row in query.all():
            total_sales_sum += Decimal(str(row.total_sales or 0))
            rows.append(
                {
                    "customer_name": row.customer_name,
                    "invoices_count": int(row.invoices_count or 0),
                    "qty_sum": float(row.qty_sum or 0),
                    "net_sales": float(row.total_sales or 0),
                    "total_sales": float(row.total_sales or 0),
                }
            )
        totals = {
            "total_sales_sum": float(total_sales_sum),
            "customers_count": len(rows),
        }

    elif report_key == "rep-sales-summary":
        columns = [
            {"key": "rep_name", "label_ar": "المندوب"},
            {"key": "invoices_count", "label_ar": "عدد الفواتير"},
            {"key": "customers_count", "label_ar": "عدد العملاء"},
            {"key": "net_sales", "label_ar": "الصافي"},
            {"key": "total_sales", "label_ar": "إجمالي المبيعات"},
        ]
        query = (
            db.query(
                Representative.id.label("rep_id"),
                Representative.name.label("rep_name"),
                func.count(func.distinct(Transfer.id)).label("invoices_count"),
                func.count(func.distinct(Transfer.to_location_id)).label("customers_count"),
                func.coalesce(func.sum(TransferLine.line_total), 0).label("total_sales"),
            )
            .join(Transfer, Transfer.rep_id == Representative.id)
            .join(TransferLine, TransferLine.transfer_id == Transfer.id)
            .filter(Transfer.kind == "sale")
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if rep_id and str(rep_id).isdigit():
            query = query.filter(Representative.id == int(rep_id))
        query = query.group_by(Representative.id, Representative.name)
        query = query.order_by(Representative.name.desc() if sort_dir == "desc" else Representative.name.asc())

        total_sales_sum = Decimal("0")
        for row in query.all():
            total_sales_sum += Decimal(str(row.total_sales or 0))
            rows.append(
                {
                    "rep_name": row.rep_name,
                    "invoices_count": int(row.invoices_count or 0),
                    "customers_count": int(row.customers_count or 0),
                    "net_sales": float(row.total_sales or 0),
                    "total_sales": float(row.total_sales or 0),
                }
            )
        totals = {
            "total_sales_sum": float(total_sales_sum),
            "reps_count": len(rows),
        }

    elif report_key == "sales-profit-report":
        columns = [
            {"key": "item_code", "label_ar": "كود الصنف"},
            {"key": "item_name", "label_ar": "اسم الصنف"},
            {"key": "qty_sold", "label_ar": "الكمية"},
            {"key": "total_sales", "label_ar": "إجمالي المبيعات"},
            {"key": "total_cost", "label_ar": "إجمالي التكلفة"},
            {"key": "profit_amount", "label_ar": "الربح"},
            {"key": "profit_margin", "label_ar": "هامش الربح %"},
        ]
        query = (
            db.query(
                Item.id.label("item_id"),
                Item.name.label("item_name"),
                func.coalesce(func.sum(TransferLine.requested_qty), 0).label("qty_sum"),
                func.coalesce(func.sum(TransferLine.line_total), 0).label("total_sales"),
                func.coalesce(func.sum(TransferLine.requested_qty * Item.purchase_price), 0).label("total_cost"),
            )
            .join(TransferLine, TransferLine.item_id == Item.id)
            .join(Transfer, TransferLine.transfer_id == Transfer.id)
            .filter(Transfer.kind == "sale")
        )
        if start_dt:
            query = query.filter(Transfer.date >= start_dt)
        if end_dt:
            query = query.filter(Transfer.date <= end_dt)
        if item_id and str(item_id).isdigit():
            query = query.filter(Item.id == int(item_id))
        if category_id and str(category_id).isdigit():
            query = query.filter(Item.category_id == int(category_id))
        query = query.group_by(Item.id, Item.name)
        query = query.order_by(Item.name.desc() if sort_dir == "desc" else Item.name.asc())

        total_sales_sum = Decimal("0")
        total_cost_sum = Decimal("0")
        total_profit_sum = Decimal("0")
        for row in query.all():
            total_sales = Decimal(str(row.total_sales or 0))
            total_cost = Decimal(str(row.total_cost or 0))
            profit = total_sales - total_cost
            margin = (profit / total_sales * Decimal("100")) if total_sales > 0 else Decimal("0")
            total_sales_sum += total_sales
            total_cost_sum += total_cost
            total_profit_sum += profit
            rows.append(
                {
                    "item_code": str(row.item_id),
                    "item_name": row.item_name,
                    "qty_sold": float(row.qty_sum or 0),
                    "total_sales": float(total_sales),
                    "total_cost": float(total_cost),
                    "profit_amount": float(profit),
                    "profit_margin": float(margin),
                }
            )
        totals = {
            "total_sales_sum": float(total_sales_sum),
            "total_cost_sum": float(total_cost_sum),
            "total_profit_sum": float(total_profit_sum),
        }

    return JSONResponse({"title_ar": title_ar, "columns": columns, "rows": rows, "totals": totals})


@app.get("/reports/profit", response_class=HTMLResponse)
async def report_profit(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    sales_query = db.query(func.coalesce(func.sum(Transfer.total), 0)).filter(
        Transfer.kind == "sale"
    )
    returns_query = db.query(func.coalesce(func.sum(Transfer.total), 0)).filter(
        Transfer.kind == "sale_return"
    )
    purchase_query = db.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
        Purchase.kind == "purchase"
    )
    purchase_returns_query = db.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
        Purchase.kind == "purchase_return"
    )
    salaries_query = db.query(func.coalesce(func.sum(EmployeeSalary.amount), 0))
    doctor_commission_query = db.query(func.coalesce(func.sum(DoctorTransaction.amount), 0)).filter(
        DoctorTransaction.type == "commission_earned"
    )

    if start_date:
        start_dt = parse_date(start_date)
        sales_query = sales_query.filter(Transfer.date >= start_dt)
        returns_query = returns_query.filter(Transfer.date >= start_dt)
        purchase_query = purchase_query.filter(Purchase.date >= start_dt)
        purchase_returns_query = purchase_returns_query.filter(Purchase.date >= start_dt)
        salaries_query = salaries_query.filter(EmployeeSalary.date >= start_dt.date())
        doctor_commission_query = doctor_commission_query.filter(DoctorTransaction.date >= start_dt)
    if end_date:
        end_dt = parse_date_end(end_date)
        sales_query = sales_query.filter(Transfer.date <= end_dt)
        returns_query = returns_query.filter(Transfer.date <= end_dt)
        purchase_query = purchase_query.filter(Purchase.date <= end_dt)
        purchase_returns_query = purchase_returns_query.filter(Purchase.date <= end_dt)
        salaries_query = salaries_query.filter(EmployeeSalary.date <= end_dt.date())
        doctor_commission_query = doctor_commission_query.filter(DoctorTransaction.date <= end_dt)

    sales_total = Decimal(str(sales_query.scalar() or 0))
    returns_total = Decimal(str(returns_query.scalar() or 0))
    purchase_total = Decimal(str(purchase_query.scalar() or 0))
    purchase_returns_total = Decimal(str(purchase_returns_query.scalar() or 0))
    salaries_total = Decimal(str(salaries_query.scalar() or 0))
    doctor_commission_total = Decimal(str(doctor_commission_query.scalar() or 0))
    net_purchases = purchase_total - purchase_returns_total
    profit = (sales_total - returns_total) - net_purchases - salaries_total - doctor_commission_total

    return templates.TemplateResponse(
        "reports/profit.html",
        {
            "request": request,
            "start_date": start_date,
            "end_date": end_date,
            "sales_total": sales_total - returns_total,
            "purchase_total": net_purchases,
            "salaries_total": salaries_total,
            "doctor_commission_total": doctor_commission_total,
            "profit": profit,
            "active_page": "reports",
        },
    )


@app.get("/reports/sales", response_class=HTMLResponse)
async def report_sales(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    customer_id: str = "",
    rep_id: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    query = db.query(Transfer).filter(Transfer.kind == "sale")
    if start_date:
        query = query.filter(Transfer.date >= parse_date(start_date))
    if end_date:
        query = query.filter(Transfer.date <= parse_date_end(end_date))
    if customer_id:
        query = query.filter(Transfer.to_location_id == int(customer_id))
    if rep_id:
        query = query.filter(Transfer.rep_id == int(rep_id))
    rows = query.order_by(Transfer.id.desc()).all()
    total = sum((Decimal(str(r.total or 0)) for r in rows), Decimal("0"))
    customers = db.query(Location).order_by(Location.name.asc()).all()
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "reports/sales.html",
        {
            "request": request,
            "rows": rows,
            "total": total,
            "start_date": start_date,
            "end_date": end_date,
            "customers": customers,
            "reps": reps,
            "selected_customer": int(customer_id) if customer_id else None,
            "selected_rep": int(rep_id) if rep_id else None,
            "active_page": "reports",
        },
    )


@app.get("/reports/sales-collections", response_class=HTMLResponse)
async def report_sales_collections(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    rep_id: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    query = db.query(SalesInvoicePayment)
    if start_date:
        query = query.filter(SalesInvoicePayment.date >= parse_date(start_date))
    if end_date:
        query = query.filter(SalesInvoicePayment.date <= parse_date_end(end_date))
    if rep_id:
        query = query.filter(SalesInvoicePayment.rep_id == int(rep_id))
    rows = query.order_by(SalesInvoicePayment.id.desc()).all()
    total = sum((Decimal(str(r.amount or 0)) for r in rows), Decimal("0"))
    reps = db.query(Representative).order_by(Representative.name.asc()).all()
    return templates.TemplateResponse(
        "reports/sales_collections.html",
        {
            "request": request,
            "rows": rows,
            "total": total,
            "start_date": start_date,
            "end_date": end_date,
            "reps": reps,
            "selected_rep": int(rep_id) if rep_id else None,
            "active_page": "reports",
        },
    )


@app.get("/reports/purchases", response_class=HTMLResponse)
async def report_purchases(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    supplier_id: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    query = db.query(Purchase).filter(Purchase.kind == "purchase")
    if start_date:
        query = query.filter(Purchase.date >= parse_date(start_date))
    if end_date:
        query = query.filter(Purchase.date <= parse_date_end(end_date))
    if supplier_id:
        query = query.filter(Purchase.supplier_id == int(supplier_id))
    rows = query.order_by(Purchase.id.desc()).all()
    total = sum((Decimal(str(r.total or 0)) for r in rows), Decimal("0"))
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    return templates.TemplateResponse(
        "reports/purchases.html",
        {
            "request": request,
            "rows": rows,
            "total": total,
            "start_date": start_date,
            "end_date": end_date,
            "suppliers": suppliers,
            "selected_supplier": int(supplier_id) if supplier_id else None,
            "active_page": "reports",
        },
    )


@app.get("/reports/purchase-payments", response_class=HTMLResponse)
async def report_purchase_payments(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    supplier_id: str = "",
    db: Session = Depends(get_db),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    query = db.query(PurchaseInvoicePayment)
    if start_date:
        query = query.filter(PurchaseInvoicePayment.date >= parse_date(start_date))
    if end_date:
        query = query.filter(PurchaseInvoicePayment.date <= parse_date_end(end_date))
    if supplier_id:
        query = query.filter(PurchaseInvoicePayment.purchase.has(Purchase.supplier_id == int(supplier_id)))
    rows = query.order_by(PurchaseInvoicePayment.id.desc()).all()
    total = sum((Decimal(str(r.amount or 0)) for r in rows), Decimal("0"))
    suppliers = db.query(Supplier).order_by(Supplier.name.asc()).all()
    return templates.TemplateResponse(
        "reports/purchase_payments.html",
        {
            "request": request,
            "rows": rows,
            "total": total,
            "start_date": start_date,
            "end_date": end_date,
            "suppliers": suppliers,
            "selected_supplier": int(supplier_id) if supplier_id else None,
            "active_page": "reports",
        },
    )


# -------------------------
# Settings + Backup
# -------------------------
from .cloudinary_backup import backup_mgr

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request,user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    settings = load_settings()
    backups = list_backups()
    cloud_status = backup_mgr.status()
    return templates.TemplateResponse(
        "settings/page.html",
        {
            "request": request,
            "settings": settings,
            "backups": backups,
            "cloud_status": cloud_status,
            "active_page": "settings",
        },
    )

@app.post("/settings/save", response_class=HTMLResponse)
async def settings_save(
    request: Request,
    company_name: str = Form(""),
    address: str = Form(""),
    phone: str = Form(""),
    logo_path: str = Form(""),
    print_paper_size: str = Form("A4"),
    user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    settings = {
        "company_name": clean_text(company_name) or "",
        "address": clean_text(address) or "",
        "phone": clean_text(phone) or "",
        "logo_path": clean_text(logo_path) or "",
        "print_paper_size": clean_text(print_paper_size) or "A4",
    }
    save_settings(settings)
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/backup", response_class=HTMLResponse)
async def settings_backup(request: Request,user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    backup_db()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/restore", response_class=HTMLResponse)
async def settings_restore(request: Request, filename: str = Form(...),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    restore_db(filename)
    return RedirectResponse(url="/settings", status_code=303)


# -------------------------
# Upload Database
# -------------------------
DB_FILE = (Path(BASE_DIR) / ".." / "erp.db").resolve()  # backend/erp.db
BACKUP_DIR = (Path(BASE_DIR) / "backups").resolve()
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

@app.post("/settings/cloud/backup", response_class=HTMLResponse)
async def settings_cloud_backup(request: Request,user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    backup_mgr.upload_rotate_two_versions(str(DB_FILE))
    return RedirectResponse(url="/settings", status_code=303)

@app.post("/settings/cloud/restore", response_class=HTMLResponse)
async def settings_cloud_restore(request: Request, which: str = Form("current"),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    which = (which or "current").lower().strip()

    tmp_name = f"cloud_{which}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    tmp_path = BACKUP_DIR / tmp_name
    backup_mgr.download(which=which, dest_file_path=str(tmp_path))

    safety = BACKUP_DIR / f"before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    if DB_FILE.exists():
        shutil.copy2(DB_FILE, safety)

    shutil.copy2(tmp_path, DB_FILE)
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/settings/cloud/status")
async def settings_cloud_status(user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    return backup_mgr.status()

# -------------------------
# Print pages
# -------------------------
@app.get("/print/{page}", response_class=HTMLResponse)
async def print_page(request: Request, page: str, db: Session = Depends(get_db),user = Depends(require_user)):
    if isinstance(user, RedirectResponse):
        return user
    print_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    settings = get_print_settings()
    if page == "items":
        items = db.query(Item).order_by(Item.id.desc()).all()
        main_wh = get_main_warehouse(db)
        stock_map = build_stock_map(db, main_wh.id)
        return templates.TemplateResponse(
            "print/items.html",
            {
                "request": request,
                "items": items,
                "stock_map": stock_map,
                "print_date": print_date,
                "settings": settings,
            },
        )
    if page == "doctors":
        doctors = db.query(Doctor).order_by(Doctor.id.desc()).all()
        return templates.TemplateResponse(
            "print/doctors.html", {"request": request, "doctors": doctors, "print_date": print_date, "settings": settings}
        )
    if page == "rep-performance":
        start_date = request.query_params.get("start_date", "")
        end_date = request.query_params.get("end_date", "")
        query = db.query(
            Transfer.rep_id,
            func.coalesce(func.sum(Transfer.total), 0).label("total"),
            func.count(Transfer.id).label("count"),
        ).filter(Transfer.kind == "sale")
        if start_date:
            query = query.filter(Transfer.date >= parse_date(start_date))
        if end_date:
            query = query.filter(Transfer.date <= parse_date_end(end_date))
        rows = query.group_by(Transfer.rep_id).order_by(func.sum(Transfer.total).desc()).all()
        reps = {r.id: r for r in db.query(Representative).all()}
        return templates.TemplateResponse(
            "print/rep_performance.html",
            {
                "request": request,
                "rows": rows,
                "reps": reps,
                "start_date": start_date,
                "end_date": end_date,
                "print_date": print_date,
                "settings": settings,
            },
        )
    if page == "region-sales":
        start_date = request.query_params.get("start_date", "")
        end_date = request.query_params.get("end_date", "")
        region = request.query_params.get("region", "")
        region_rows = (
            db.query(
                Location.region,
                func.coalesce(func.sum(Transfer.total), 0).label("total"),
                func.count(Transfer.id).label("count"),
            )
            .join(Location, Transfer.to_location_id == Location.id)
            .filter(
                Transfer.kind == "sale",
                Location.region.isnot(None),
                Location.region != "",
            )
        )
        if start_date:
            region_rows = region_rows.filter(Transfer.date >= parse_date(start_date))
        if end_date:
            region_rows = region_rows.filter(Transfer.date <= parse_date_end(end_date))
        region_rows = region_rows.group_by(Location.region).order_by(func.sum(Transfer.total).desc()).all()

        location_rows = []
        if region:
            location_rows = (
                db.query(
                    Location.id,
                    Location.name,
                    Location.type,
                    func.coalesce(func.sum(Transfer.total), 0).label("total"),
                    func.count(Transfer.id).label("count"),
                )
                .join(Location, Transfer.to_location_id == Location.id)
                .filter(Transfer.kind == "sale", Location.region == region)
                .group_by(Location.id, Location.name, Location.type)
                .order_by(func.sum(Transfer.total).desc())
                .all()
            )
        return templates.TemplateResponse(
            "print/region_sales.html",
            {
                "request": request,
                "region_rows": region_rows,
                "location_rows": location_rows,
                "selected_region": region,
                "start_date": start_date,
                "end_date": end_date,
                "print_date": print_date,
                "settings": settings,
            },
        )
    if page == "profit":
        start_date = request.query_params.get("start_date", "")
        end_date = request.query_params.get("end_date", "")
        sales_query = db.query(func.coalesce(func.sum(Transfer.total), 0)).filter(
            Transfer.kind == "sale"
        )
        returns_query = db.query(func.coalesce(func.sum(Transfer.total), 0)).filter(
            Transfer.kind == "sale_return"
        )
        purchase_query = db.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
            Purchase.kind == "purchase"
        )
        purchase_returns_query = db.query(func.coalesce(func.sum(Purchase.total), 0)).filter(
            Purchase.kind == "purchase_return"
        )

        if start_date:
            start_dt = parse_date(start_date)
            sales_query = sales_query.filter(Transfer.date >= start_dt)
            returns_query = returns_query.filter(Transfer.date >= start_dt)
            purchase_query = purchase_query.filter(Purchase.date >= start_dt)
            purchase_returns_query = purchase_returns_query.filter(Purchase.date >= start_dt)
        if end_date:
            end_dt = parse_date_end(end_date)
            sales_query = sales_query.filter(Transfer.date <= end_dt)
            returns_query = returns_query.filter(Transfer.date <= end_dt)
            purchase_query = purchase_query.filter(Purchase.date <= end_dt)
            purchase_returns_query = purchase_returns_query.filter(Purchase.date <= end_dt)

        sales_total = Decimal(str(sales_query.scalar() or 0))
        returns_total = Decimal(str(returns_query.scalar() or 0))
        purchase_total = Decimal(str(purchase_query.scalar() or 0))
        purchase_returns_total = Decimal(str(purchase_returns_query.scalar() or 0))
        net_purchases = purchase_total - purchase_returns_total
        profit = (sales_total - returns_total) - net_purchases
        return templates.TemplateResponse(
            "print/profit.html",
            {
                "request": request,
                "print_date": print_date,
                "start_date": start_date,
                "end_date": end_date,
                "sales_total": sales_total - returns_total,
                "purchase_total": net_purchases,
                "profit": profit,
                "settings": settings,
            },
        )
    return RedirectResponse(url="/")







