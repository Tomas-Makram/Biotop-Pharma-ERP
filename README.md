# 🧬 Biotop Pharma ERP

![Biotop Pharma](logo.png)

## Overview

Biotop Pharma ERP is a comprehensive pharmaceutical distribution and inventory management system designed specifically for pharmacies, drug distributors, medical warehouses, and healthcare supply chains.

The system streamlines inventory operations, purchasing, sales, supplier management, customer accounts, cash flow tracking, employee management, and business reporting through a modern and efficient web-based interface.

Built with FastAPI, SQLAlchemy, SQLite, and Jinja2 Templates.

---

# ✨ Key Features

## 📦 Inventory Management

* Product management
* Multiple units per item
* Categories and classifications
* Batch / Lot tracking
* Expiry date management
* Opening stock entries
* Damage and wastage management
* Real-time stock balances
* Inventory movement tracking

---

## 🏢 Warehouse Management

* Main warehouse support
* Multiple locations support
* Internal stock transfers
* Transfer allocations
* Stock reconciliation
* Stocktaking operations

---

## 🛒 Purchasing Management

* Purchase invoices
* Purchase returns
* Supplier balances
* Supplier ledger
* Supplier adjustments
* Purchase payment tracking
* Shipping expense allocation

---

## 💰 Financial Operations

* Cash accounts management
* Main cash treasury
* Representative cash accounts
* Cash transactions
* Expense tracking
* Customer balances
* Supplier balances
* Opening balances

---

## 🚚 Distribution & Sales

* Customer management
* Pharmacy management
* Sales orders
* Distribution tracking
* Representative assignment
* Customer account statements

---

## 👨‍⚕️ Medical Representative Management

* Representative profiles
* Territory assignment
* Commission management
* Salary management
* Employee deductions
* Employee additions
* Payroll tracking

---

## 🩺 Doctor Management

* Doctor database
* Doctor commission rules
* Doctor commission transactions
* Product-based commission calculations

---

## 🔐 User & Permissions System

* Authentication system
* Session management
* User roles
* Granular permissions
* Admin panel
* Password hashing using PBKDF2

---

## ☁️ Backup System

* Local database backups
* Cloud backups
* Cloudinary integration
* Backup rotation
* Database restore functionality

---

## 📊 Reporting

* Stock reports
* Inventory movement reports
* Customer statements
* Supplier statements
* Financial reports
* Operational reports

---

# 🛠️ Technology Stack

### Backend

* FastAPI
* SQLAlchemy
* SQLite
* Jinja2
* Passlib

### Frontend

* HTML5
* CSS3
* JavaScript
* Jinja Templates

### Storage

* SQLite Database

### Backup

* Cloudinary

---

# 📁 Project Structure

```text
backend/
│
├── app/
│   ├── main.py
│   ├── models.py
│   ├── repo.py
│   ├── auth.py
│   ├── db.py
│   └── cloudinary_backup.py
│
├── templates/
├── static/
├── backups/
├── config.json
└── erp.db
```

---

# 🚀 Installation

```bash
git clone <repository-url>

cd Biotop-Pharma-ERP

pip install -r requirements.txt

uvicorn backend.app.main:app --reload
```

---

# 🌐 Access

After running:

```bash
http://127.0.0.1:8000
```

---

# 🔒 Security Notes

Before publishing:

* Remove all API secrets.
* Remove cloud credentials.
* Remove production database files.
* Remove backup files.
* Remove local configuration files.
* Create new secrets if any credentials were previously exposed.

---

# 📜 License

This project is intended for educational, commercial, and pharmaceutical management purposes.

---

# 👨‍💻 Developed By

Biotop Pharma ERP Team

Built to simplify pharmaceutical distribution, inventory control, and financial operations.
