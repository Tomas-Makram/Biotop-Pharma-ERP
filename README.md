# ERP (FastAPI + SQLite)

## التشغيل محليا

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

- قاعدة البيانات: `backend/erp.db`
- النسخ الاحتياطية: `backend/backups/`

## ملاحظات
- الواجهة عربية RTL باستخدام Bootstrap 5 RTL.
- جميع البيانات تعمل محليا عبر SQLite.

## بناء ملف تنفيذي (اختياري)
يمكن استخدام PyInstaller لإنشاء ملف تنفيذي:

```powershell
pip install pyinstaller
pyinstaller --onefile --name ERP backend\app\main.py
```
