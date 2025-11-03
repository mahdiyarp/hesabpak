# -*- coding: utf-8 -*-
from flask import Blueprint, request, jsonify
from sqlalchemy import or_
from yourapp import db
from yourapp.models import Item, Person  # طبق مدل‌های خودت تنظیم کن

bp_search = Blueprint("bp_search", __name__, url_prefix="/api")

@bp_search.get("/search")
def unified_search():
    """
    ?type=item|customer|vendor  &q=...
    خروجی: {results:[{id,code,name,stock,balance,price,extra}]}
    """
    q = (request.args.get("q") or "").strip()
    typ = (request.args.get("type") or "item").strip().lower()

    if not q:
        return jsonify({"results": []})

    results = []
    if typ == "item":
        # براساس فیلدهای واقعی خودت تنظیم کن: code, name, barcode, stock, sale_price
        rows = (db.session.query(Item)
                .filter(or_(
                    Item.code.ilike(f"%{q}%"),
                    Item.name.ilike(f"%{q}%"),
                    Item.barcode.ilike(f"%{q}%"),
                ))
                .order_by(Item.name.asc())
                .limit(20).all())
        for r in rows:
            results.append({
                "id": r.id,
                "code": r.code or "",
                "name": r.name or "",
                "stock": getattr(r, "stock", None),
                "price": getattr(r, "sale_price", None),
                "extra": getattr(r, "specs", "") or "",
            })

    elif typ in ("customer", "vendor", "person"):
        # یک مدل واحد Person (یا Customer/Vendor جدا) — مطابق دیتابیس خودت تغییر بده
        rows = (db.session.query(Person)
                .filter(or_(
                    Person.code.ilike(f"%{q}%"),
                    Person.name.ilike(f"%{q}%"),
                    Person.mobile.ilike(f"%{q}%"),
                ))
                .order_by(Person.name.asc())
                .limit(20).all())
        for r in rows:
            results.append({
                "id": r.id,
                "code": r.code or "",
                "name": r.name or "",
                "balance": getattr(r, "balance", 0.0),
                "extra": getattr(r, "notes", "") or "",
            })
    else:
        return jsonify({"results": []})

    return jsonify({"results": results})
