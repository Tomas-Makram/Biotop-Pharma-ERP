from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from pathlib import Path
from sqlalchemy import text
import json
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text


BASE_DIR = Path(__file__).resolve().parents[1]  # backend/
DB_PATH = BASE_DIR / "erp.db"
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    full_name = Column(String(200), default="", nullable=False)
    password_hash = Column(String(255), nullable=False)

    # Administration
    is_admin = Column(Boolean, default=False, nullable=False)

    # Perimation (permissions) stored as JSON text: ["items.read", "items.write", ...]
    permissions = Column(Text, default="[]", nullable=False)

    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def permissions_list(self):
        try:
            return json.loads(self.permissions or "[]")
        except Exception:
            return []


def _get_table_columns(conn, table_name: str):
    rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
    return {row[1] for row in rows}


def ensure_schema():
    with engine.begin() as conn:
        existing_tables = {
            row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        if "location_reps" not in existing_tables:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS location_reps ("
                    "location_id INTEGER NOT NULL,"
                    "rep_id INTEGER NOT NULL,"
                    "PRIMARY KEY (location_id, rep_id)"
                    ")"
                )
            )
        if "items" in existing_tables:
            cols = _get_table_columns(conn, "items")
            if "code" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN code VARCHAR(100)"))
            if "unit" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN unit VARCHAR(50)"))
            if "item_kind" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN item_kind VARCHAR(20) DEFAULT 'general'"))
            if "purchase_price" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN purchase_price NUMERIC(12,2) DEFAULT 0"))
            if "sale_price" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN sale_price NUMERIC(12,2) DEFAULT 0"))
            if "is_active" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN is_active BOOLEAN DEFAULT 1"))
            if "notes" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN notes TEXT"))
            if "category_id" not in cols:
                conn.execute(text("ALTER TABLE items ADD COLUMN category_id INTEGER"))
        if "pharmacies" in existing_tables:
            cols = _get_table_columns(conn, "pharmacies")
            if "phone" not in cols:
                conn.execute(text("ALTER TABLE pharmacies ADD COLUMN phone VARCHAR(50)"))
            if "region" not in cols:
                conn.execute(text("ALTER TABLE pharmacies ADD COLUMN region VARCHAR(100)"))
            if "notes" not in cols:
                conn.execute(text("ALTER TABLE pharmacies ADD COLUMN notes TEXT"))
        if "suppliers" in existing_tables:
            cols = _get_table_columns(conn, "suppliers")
            if "contact_name" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN contact_name VARCHAR(200)"))
            if "contact_phone" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN contact_phone VARCHAR(50)"))
            if "phone2" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN phone2 VARCHAR(50)"))
            if "fax" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN fax VARCHAR(50)"))
            if "governorate" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN governorate VARCHAR(100)"))
            if "city" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN city VARCHAR(100)"))
            if "region" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN region VARCHAR(100)"))
            if "email" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN email VARCHAR(200)"))
            if "website" not in cols:
                conn.execute(text("ALTER TABLE suppliers ADD COLUMN website VARCHAR(200)"))
        if "representatives" in existing_tables:
            cols = _get_table_columns(conn, "representatives")
            if "code" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN code VARCHAR(50)"))
            if "home_phone" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN home_phone VARCHAR(50)"))
            if "mobile" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN mobile VARCHAR(50)"))
            if "address" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN address VARCHAR(300)"))
            if "governorate" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN governorate VARCHAR(100)"))
            if "city" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN city VARCHAR(100)"))
            if "region" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN region VARCHAR(100)"))
            if "birth_date" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN birth_date DATE"))
            if "gender" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN gender VARCHAR(20)"))
            if "national_id" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN national_id VARCHAR(50)"))
            if "job_title" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN job_title VARCHAR(100)"))
            if "supervisor" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN supervisor VARCHAR(200)"))
            if "hire_date" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN hire_date DATE"))
            if "base_salary" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN base_salary NUMERIC(12,2) DEFAULT 0"))
            if "hourly_rate" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN hourly_rate NUMERIC(12,2) DEFAULT 0"))
            if "insurance_no" not in cols:
                conn.execute(text("ALTER TABLE representatives ADD COLUMN insurance_no VARCHAR(50)"))
        if "employee_additions" in existing_tables:
            cols = _get_table_columns(conn, "employee_additions")
            if "month" not in cols:
                conn.execute(text("ALTER TABLE employee_additions ADD COLUMN month INTEGER DEFAULT 0"))
            if "year" not in cols:
                conn.execute(text("ALTER TABLE employee_additions ADD COLUMN year INTEGER DEFAULT 0"))
        if "employee_deductions" in existing_tables:
            cols = _get_table_columns(conn, "employee_deductions")
            if "month" not in cols:
                conn.execute(text("ALTER TABLE employee_deductions ADD COLUMN month INTEGER DEFAULT 0"))
            if "year" not in cols:
                conn.execute(text("ALTER TABLE employee_deductions ADD COLUMN year INTEGER DEFAULT 0"))
        if "purchases" in existing_tables:
            cols = _get_table_columns(conn, "purchases")
            if "location_id" not in cols:
                conn.execute(text("ALTER TABLE purchases ADD COLUMN location_id INTEGER"))
            if "kind" not in cols:
                conn.execute(text("ALTER TABLE purchases ADD COLUMN kind VARCHAR(20) DEFAULT 'purchase'"))
        if "purchase_items" in existing_tables:
            cols = _get_table_columns(conn, "purchase_items")
            if "bonus_qty" not in cols:
                conn.execute(text("ALTER TABLE purchase_items ADD COLUMN bonus_qty NUMERIC(12,2) DEFAULT 0"))
            if "tax_amount" not in cols:
                conn.execute(text("ALTER TABLE purchase_items ADD COLUMN tax_amount NUMERIC(12,2) DEFAULT 0"))
            if "discount_base" not in cols:
                conn.execute(text("ALTER TABLE purchase_items ADD COLUMN discount_base NUMERIC(12,2) DEFAULT 0"))
            if "discount_extra" not in cols:
                conn.execute(text("ALTER TABLE purchase_items ADD COLUMN discount_extra NUMERIC(12,2) DEFAULT 0"))
            if "line_total" not in cols:
                conn.execute(text("ALTER TABLE purchase_items ADD COLUMN line_total NUMERIC(12,2) DEFAULT 0"))
        if "item_lots" in existing_tables:
            cols = _get_table_columns(conn, "item_lots")
            if "expiry_date" not in cols:
                conn.execute(text("ALTER TABLE item_lots ADD COLUMN expiry_date DATE"))
            if "purchase_price" not in cols:
                conn.execute(text("ALTER TABLE item_lots ADD COLUMN purchase_price NUMERIC(12,2)"))
            if "created_at" not in cols:
                conn.execute(text("ALTER TABLE item_lots ADD COLUMN created_at DATETIME"))
        if "doctors" in existing_tables:
            cols = _get_table_columns(conn, "doctors")
            if "notes" not in cols:
                conn.execute(text("ALTER TABLE doctors ADD COLUMN notes TEXT"))
        if "doctor_commission_rules" in existing_tables:
            cols = _get_table_columns(conn, "doctor_commission_rules")
            if "notes" not in cols:
                conn.execute(text("ALTER TABLE doctor_commission_rules ADD COLUMN notes TEXT"))
        if "location_transactions" in existing_tables:
            cols = _get_table_columns(conn, "location_transactions")
            if "created_at" not in cols:
                conn.execute(text("ALTER TABLE location_transactions ADD COLUMN created_at DATETIME"))
            if "receipt_no" not in cols:
                conn.execute(text("ALTER TABLE location_transactions ADD COLUMN receipt_no VARCHAR(100)"))
            if "prev_balance_snapshot" not in cols:
                conn.execute(text("ALTER TABLE location_transactions ADD COLUMN prev_balance_snapshot NUMERIC(12,2)"))
            if "new_balance_snapshot" not in cols:
                conn.execute(text("ALTER TABLE location_transactions ADD COLUMN new_balance_snapshot NUMERIC(12,2)"))
        if "supplier_transactions" not in existing_tables:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS supplier_transactions ("
                    "id INTEGER PRIMARY KEY,"
                    "date DATETIME NOT NULL,"
                    "created_at DATETIME NOT NULL,"
                    "supplier_id INTEGER NOT NULL,"
                    "type VARCHAR(50) NOT NULL,"
                    "amount NUMERIC(12,2) NOT NULL DEFAULT 0,"
                    "prev_balance_snapshot NUMERIC(12,2),"
                    "new_balance_snapshot NUMERIC(12,2),"
                    "notes TEXT,"
                    "source_type VARCHAR(50),"
                    "source_id INTEGER"
                    ")"
                )
            )
        if "other_expenses" not in existing_tables:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS other_expenses ("
                    "id INTEGER PRIMARY KEY,"
                    "date DATETIME NOT NULL,"
                    "title VARCHAR(200) NOT NULL,"
                    "amount NUMERIC(12,2) NOT NULL DEFAULT 0,"
                    "notes TEXT,"
                    "source_type VARCHAR(50),"
                    "source_id INTEGER,"
                    "created_at DATETIME NOT NULL"
                    ")"
                )
            )
        if "stock_opening_lines" in existing_tables:
            cols = _get_table_columns(conn, "stock_opening_lines")
            if "unit_price" not in cols:
                conn.execute(text("ALTER TABLE stock_opening_lines ADD COLUMN unit_price NUMERIC(12,2) DEFAULT 0"))
            if "discount_percent" not in cols:
                conn.execute(text("ALTER TABLE stock_opening_lines ADD COLUMN discount_percent NUMERIC(5,2) DEFAULT 0"))
            if "discount_amount" not in cols:
                conn.execute(text("ALTER TABLE stock_opening_lines ADD COLUMN discount_amount NUMERIC(12,2) DEFAULT 0"))
            if "line_total" not in cols:
                conn.execute(text("ALTER TABLE stock_opening_lines ADD COLUMN line_total NUMERIC(12,2) DEFAULT 0"))
        if "transfers" in existing_tables:
            cols = _get_table_columns(conn, "transfers")
            if "kind" not in cols:
                conn.execute(text("ALTER TABLE transfers ADD COLUMN kind VARCHAR(20) DEFAULT 'transfer'"))
            if "price_category" not in cols:
                conn.execute(text("ALTER TABLE transfers ADD COLUMN price_category VARCHAR(50)"))
        if "transfer_lines" in existing_tables:
            cols = _get_table_columns(conn, "transfer_lines")
            if "bonus_amount" not in cols:
                conn.execute(text("ALTER TABLE transfer_lines ADD COLUMN bonus_amount NUMERIC(12,2) DEFAULT 0"))
            if "discount_amount" not in cols:
                conn.execute(text("ALTER TABLE transfer_lines ADD COLUMN discount_amount NUMERIC(12,2) DEFAULT 0"))
            if "line_total" not in cols:
                conn.execute(text("ALTER TABLE transfer_lines ADD COLUMN line_total NUMERIC(12,2) DEFAULT 0"))
                # -------------------------
        if "locations" in existing_tables and "location_reps" in {
            row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }:
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO location_reps (location_id, rep_id) "
                    "SELECT id, rep_id FROM locations WHERE rep_id IS NOT NULL"
                )
            )
        # -------------------------
        # users table (auth)
        # -------------------------
        if "users" not in existing_tables:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS users ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "full_name VARCHAR(200) NOT NULL DEFAULT,"
                    "username VARCHAR(100) NOT NULL UNIQUE,"
                    "password_hash VARCHAR(255) NOT NULL,"
                    "is_admin BOOLEAN NOT NULL DEFAULT 0,"
                    "permissions TEXT NOT NULL DEFAULT '[]',"
                    "is_active BOOLEAN NOT NULL DEFAULT 1,"
                    "created_at DATETIME NOT NULL"
                    ")"
                )
            )
        if "users" in existing_tables:
            cols = _get_table_columns(conn, "users")
            if "full_name" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN full_name VARCHAR(200) NOT NULL DEFAULT ''"))

        else:
            cols = _get_table_columns(conn, "users")
            if "is_admin" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
            if "permissions" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'"))
            if "is_active" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1"))
            if "created_at" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME"))
