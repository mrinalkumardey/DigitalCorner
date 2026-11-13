"""GST module for Digital Corner (v2: item catalogue).

In app.py, before `if __name__ == '__main__':`
    from gst_routes import init_gst
    init_gst(app, supabase, login_required, ALLOWED_FUNDS)
"""
import datetime
import logging
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP

from flask import jsonify, render_template, request

log = logging.getLogger("gst")

SUPPLIER = {
    "name": "Digital Corner",
    "gstin": "18EQRPD1182B1ZY",
    "address": "Laluk Bus Stand, Laluk, North Lakhimpur, Assam, 784160",
    "phone": "+91 8811008135",
    "state_code": "18",
}

STATES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra", "29": "Karnataka", "30": "Goa",
    "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman & Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}

# Ledger headings usable on GST invoices, by item type. Add a heading here (and in the
# dashboard's category list) before it can be used in Settings.
CATALOGUE_CATEGORIES = {
    "services": ["Tech & Web Solutions", "Service Fees"],
    "goods": ["Stationery & Printing"],
}
ALL_CATEGORIES = {c for v in CATALOGUE_CATEGORIES.values() for c in v}
DOUBLE_ENTRY = {"Tech & Web Solutions"}  # categories that may carry a portal/vendor cost

GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
CENT = Decimal("0.01")


def money(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def fy_label(d):
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y % 100:02d}-{(y + 1) % 100:02d}"


def calc_line(qty, price, rate, mode, inter_state):
    base = money(qty * price)
    r = Decimal(str(rate))
    if mode == "exclusive":
        taxable = base
        gst = money(taxable * r / 100)
        total = taxable + gst
    else:
        total = base
        taxable = money(total * 100 / (100 + r))
        gst = total - taxable
    if inter_state:
        cgst = sgst = Decimal("0.00")
        igst = gst
    else:
        cgst = money(gst / 2)
        sgst = gst - cgst
        igst = Decimal("0.00")
    return taxable, cgst, sgst, igst, total


def _parse_date(s, field):
    try:
        return datetime.date.fromisoformat(s)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field}")


def _uuid(s, field="id"):
    try:
        return str(uuid.UUID(str(s)))
    except ValueError:
        raise ValueError(f"Invalid {field}")


def init_gst(app, supabase, login_required, allowed_funds):

    def effective_rates(as_of):
        rows = (supabase.table("gst_rates").select("*").eq("is_active", True)
                .lte("effective_from", as_of).order("effective_from", desc=True).execute().data)
        seen, out = set(), []
        for r in rows:
            if r["hsn_sac"] not in seen:
                seen.add(r["hsn_sac"])
                out.append(r)
        return sorted(out, key=lambda r: (r["item_type"], r["hsn_sac"]))

    def items_with_rates(as_of, only_active):
        q = supabase.table("gst_items").select("*")
        if only_active:
            q = q.eq("is_active", True)
        rows = q.order("item_type").order("category").order("name").execute().data
        rate_map = {r["hsn_sac"]: r["rate"] for r in effective_rates(as_of)}
        for r in rows:
            r["rate"] = rate_map.get(r["hsn_sac"])
        return rows

    # ------------------------------ pages ------------------------------
    @app.route("/gst-invoice")
    @login_required
    def gst_invoice_page():
        return render_template("gst_invoice.html", supplier=SUPPLIER, states=STATES,
                               double_entry=sorted(DOUBLE_ENTRY), funds=allowed_funds, active="gst")

    @app.route("/settings")
    @login_required
    def settings_page():
        return render_template("settings.html", active="settings")

    @app.route("/settings/gst-rates")
    @login_required
    def gst_rates_page():
        return render_template("gst_rates.html", active="settings")

    @app.route("/settings/gst-items")
    @login_required
    def gst_items_page():
        return render_template("gst_items.html", categories=CATALOGUE_CATEGORIES, active="settings")

    # ------------------------------ rates API ------------------------------
    @app.route("/api/gst/rates", methods=["GET"])
    @login_required
    def api_gst_rates():
        try:
            as_of = _parse_date(request.args.get("as_of") or datetime.date.today().isoformat(), "as_of")
            return jsonify({"rates": effective_rates(as_of.isoformat())}), 200
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            log.exception("rates fetch failed")
            return jsonify({"error": "Could not load GST rates"}), 500

    @app.route("/api/gst/rates/all", methods=["GET"])
    @login_required
    def api_gst_rates_all():
        try:
            rows = (supabase.table("gst_rates").select("*")
                    .order("hsn_sac").order("effective_from", desc=True).execute().data)
            return jsonify({"rates": rows, "today": datetime.date.today().isoformat()}), 200
        except Exception:
            log.exception("rate history failed")
            return jsonify({"error": "Could not load rate history"}), 500

    @app.route("/api/gst/rates", methods=["POST"])
    @login_required
    def api_gst_rate_add():
        d = request.json or {}
        try:
            hsn = str(d.get("hsn_sac", "")).strip()
            if not re.fullmatch(r"\d{4,8}", hsn):
                raise ValueError("HSN/SAC must be 4-8 digits")
            item_type = d.get("item_type")
            if item_type not in ("goods", "services"):
                raise ValueError("Type must be goods or services")
            desc = str(d.get("description", "")).strip()
            if not desc or len(desc) > 200:
                raise ValueError("Description is required (max 200 chars)")
            try:
                rate = Decimal(str(d.get("rate")))
            except ArithmeticError:
                raise ValueError("Invalid rate")
            if rate < 0 or rate > 100:
                raise ValueError("Rate must be between 0 and 100")
            eff = _parse_date(d.get("effective_from"), "effective date")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            supabase.table("gst_rates").insert({
                "hsn_sac": hsn, "item_type": item_type, "description": desc,
                "rate": str(rate), "effective_from": eff.isoformat()}).execute()
            return jsonify({"message": "Rate saved"}), 201
        except Exception as e:
            log.exception("rate insert failed")
            dup = "duplicate" in str(e).lower()
            return jsonify({"error": "A rate for this code already exists on that effective date" if dup
                            else "Could not save rate"}), (409 if dup else 500)

    @app.route("/api/gst/rates/<rid>/toggle", methods=["POST"])
    @login_required
    def api_gst_rate_toggle(rid):
        try:
            rid = _uuid(rid)
            row = supabase.table("gst_rates").select("is_active").eq("id", rid).single().execute().data
            supabase.table("gst_rates").update({"is_active": not row["is_active"]}).eq("id", rid).execute()
            return jsonify({"message": "Updated"}), 200
        except ValueError:
            return jsonify({"error": "Invalid id"}), 400
        except Exception:
            log.exception("rate toggle failed")
            return jsonify({"error": "Could not update rate"}), 500

    # ------------------------------ catalogue API ------------------------------
    @app.route("/api/gst/catalogue", methods=["GET"])
    @login_required
    def api_gst_catalogue():
        try:
            as_of = _parse_date(request.args.get("as_of") or datetime.date.today().isoformat(), "as_of")
            return jsonify({"categories": CATALOGUE_CATEGORIES,
                            "items": items_with_rates(as_of.isoformat(), True)}), 200
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception:
            log.exception("catalogue failed")
            return jsonify({"error": "Could not load items"}), 500

    @app.route("/api/gst/items", methods=["GET"])
    @login_required
    def api_gst_items():
        try:
            return jsonify({"items": items_with_rates(datetime.date.today().isoformat(), False)}), 200
        except Exception:
            log.exception("items list failed")
            return jsonify({"error": "Could not load items"}), 500

    def _item_fields(d):
        item_type = d.get("item_type")
        if item_type not in CATALOGUE_CATEGORIES:
            raise ValueError("Type must be goods or services")
        category = d.get("category")
        if category not in CATALOGUE_CATEGORIES[item_type]:
            raise ValueError("Choose a valid category for this type")
        name = str(d.get("name", "")).strip()
        if not name or len(name) > 150:
            raise ValueError("Item name is required (max 150 chars)")
        hsn = str(d.get("hsn_sac", "")).strip()
        if not re.fullmatch(r"\d{4,8}", hsn):
            raise ValueError("HSN/SAC must be 4-8 digits")
        return {"item_type": item_type, "category": category, "name": name, "hsn_sac": hsn}

    def _rate_warning(hsn):
        has = supabase.table("gst_rates").select("id").eq("hsn_sac", hsn).limit(1).execute().data
        return None if has else f"No GST rate is configured for {hsn}. Add one in GST Rate Configuration before using this item."

    @app.route("/api/gst/items", methods=["POST"])
    @login_required
    def api_gst_item_add():
        try:
            fields = _item_fields(request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            supabase.table("gst_items").insert(fields).execute()
            return jsonify({"message": "Item saved", "warning": _rate_warning(fields["hsn_sac"])}), 201
        except Exception as e:
            log.exception("item insert failed")
            dup = "duplicate" in str(e).lower()
            return jsonify({"error": "An item with this name already exists in that category" if dup
                            else "Could not save item"}), (409 if dup else 500)

    @app.route("/api/gst/items/<iid>", methods=["PUT"])
    @login_required
    def api_gst_item_update(iid):
        try:
            iid = _uuid(iid)
            fields = _item_fields(request.json or {})
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        try:
            supabase.table("gst_items").update(fields).eq("id", iid).execute()
            return jsonify({"message": "Item updated", "warning": _rate_warning(fields["hsn_sac"])}), 200
        except Exception as e:
            log.exception("item update failed")
            dup = "duplicate" in str(e).lower()
            return jsonify({"error": "An item with this name already exists in that category" if dup
                            else "Could not update item"}), (409 if dup else 500)

    @app.route("/api/gst/items/<iid>/toggle", methods=["POST"])
    @login_required
    def api_gst_item_toggle(iid):
        try:
            iid = _uuid(iid)
            row = supabase.table("gst_items").select("is_active").eq("id", iid).single().execute().data
            supabase.table("gst_items").update({"is_active": not row["is_active"]}).eq("id", iid).execute()
            return jsonify({"message": "Updated"}), 200
        except ValueError:
            return jsonify({"error": "Invalid id"}), 400
        except Exception:
            log.exception("item toggle failed")
            return jsonify({"error": "Could not update item"}), 500

    @app.route("/api/gst/items/<iid>", methods=["DELETE"])
    @login_required
    def api_gst_item_delete(iid):
        # Issued invoices keep their own saved copy of code/description, so deleting is safe.
        try:
            iid = _uuid(iid)
            supabase.table("gst_items").delete().eq("id", iid).execute()
            return jsonify({"message": "Item deleted"}), 200
        except ValueError:
            return jsonify({"error": "Invalid id"}), 400
        except Exception:
            log.exception("item delete failed")
            return jsonify({"error": "Could not delete item"}), 500

    # ------------------------------ invoices API ------------------------------
    def load_invoice(inv_id):
        inv = supabase.table("gst_invoices").select("*").eq("id", inv_id).single().execute().data
        inv["items"] = (supabase.table("gst_invoice_items").select("*")
                        .eq("invoice_id", inv_id).order("line_no").execute().data)
        return inv

    @app.route("/api/gst/invoices", methods=["GET"])
    @login_required
    def api_gst_invoices():
        try:
            rows = (supabase.table("gst_invoices")
                    .select("id,invoice_no,invoice_date,customer_name,total_amount,sync_to_dashboard")
                    .order("created_at", desc=True).limit(50).execute().data)
            return jsonify({"invoices": rows}), 200
        except Exception:
            log.exception("invoice list failed")
            return jsonify({"error": "Could not load invoices"}), 500

    @app.route("/api/gst/invoice/<inv_id>", methods=["GET"])
    @login_required
    def api_gst_invoice(inv_id):
        try:
            inv_id = _uuid(inv_id)
            return jsonify({"invoice": load_invoice(inv_id)}), 200
        except ValueError:
            return jsonify({"error": "Invalid id"}), 400
        except Exception:
            log.exception("invoice load failed")
            return jsonify({"error": "Invoice not found"}), 404

    @app.route("/api/gst/invoice", methods=["POST"])
    @login_required
    def api_gst_issue():
        d = request.json or {}
        try:
            request_id = _uuid(d.get("request_id"), "request id")
            invoice_date = _parse_date(d.get("invoice_date"), "invoice date")
            supply_date = _parse_date(d.get("supply_date") or d.get("invoice_date"), "supply date")
            if not d.get("fully_paid"):
                raise ValueError("GST invoices can only be issued for fully paid bills")
            customer = str(d.get("customer_name", "")).strip()
            if not customer or len(customer) > 150:
                raise ValueError("Customer name is required")
            c_gstin = str(d.get("customer_gstin", "")).strip().upper()
            if c_gstin and not GSTIN_RE.match(c_gstin):
                raise ValueError("Customer GSTIN format is invalid")
            pos_code = str(d.get("place_of_supply_code", ""))
            if pos_code not in STATES:
                raise ValueError("Select a valid place of supply")
            mode = d.get("price_mode")
            if mode not in ("inclusive", "exclusive"):
                raise ValueError("Select GST inclusive or exclusive pricing")
            fund = d.get("payment_fund")
            if fund not in allowed_funds:
                raise ValueError("Select a valid payment fund")
            sync = bool(d.get("sync"))
            raw_items = d.get("items")
            if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= 30:
                raise ValueError("Add between 1 and 30 items")

            ids = [_uuid(it.get("item_id"), "item") for it in raw_items]
            cat_rows = supabase.table("gst_items").select("*").in_("id", list(set(ids))).execute().data
            cat_map = {r["id"]: r for r in cat_rows}
            rate_map = {r["hsn_sac"]: r for r in effective_rates(supply_date.isoformat())}
            inter = pos_code != SUPPLIER["state_code"]
            items, tot = [], dict(t=Decimal(0), c=Decimal(0), s=Decimal(0), i=Decimal(0), a=Decimal(0))

            for n, (it, iid) in enumerate(zip(raw_items, ids), 1):
                cat_row = cat_map.get(iid)
                if not cat_row or not cat_row["is_active"]:
                    raise ValueError(f"Line {n}: selected item is no longer available")
                rate_row = rate_map.get(cat_row["hsn_sac"])
                if not rate_row:
                    raise ValueError(f"Line {n}: no active GST rate for {cat_row['hsn_sac']} on {supply_date}")
                desc = str(it.get("description") or cat_row["name"]).strip()
                if not desc or len(desc) > 200:
                    raise ValueError(f"Line {n}: description is required (max 200 chars)")
                try:
                    qty, price = Decimal(str(it.get("quantity"))), Decimal(str(it.get("unit_price")))
                except ArithmeticError:
                    raise ValueError(f"Line {n}: invalid quantity or price")
                if qty <= 0 or price <= 0 or qty > 100000 or price > 100000000:
                    raise ValueError(f"Line {n}: quantity and price must be positive")
                taxable, cgst, sgst, igst, total = calc_line(qty, price, rate_row["rate"], mode, inter)

                cost, cost_fund = Decimal(0), ""
                raw_cost = it.get("cost_amount")
                if sync and cat_row["category"] in DOUBLE_ENTRY and raw_cost not in (None, "", 0, "0"):
                    try:
                        cost = money(raw_cost)
                    except ArithmeticError:
                        raise ValueError(f"Line {n}: invalid cost")
                    if cost < 0:
                        raise ValueError(f"Line {n}: cost cannot be negative")
                    cost_fund = it.get("cost_fund")
                    if cost > 0 and cost_fund not in allowed_funds:
                        raise ValueError(f"Line {n}: select the fund the cost is paid from")
                items.append({
                    "item_id": iid, "description": desc, "category": cat_row["category"],
                    "item_type": cat_row["item_type"], "hsn_sac": cat_row["hsn_sac"],
                    "quantity": str(qty), "unit_price": str(money(price)), "gst_rate": str(rate_row["rate"]),
                    "taxable_value": str(taxable), "cgst": str(cgst), "sgst": str(sgst), "igst": str(igst),
                    "line_total": str(total), "cost_amount": str(cost), "cost_fund": cost_fund or ""})
                tot["t"] += taxable; tot["c"] += cgst; tot["s"] += sgst; tot["i"] += igst; tot["a"] += total
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        payload = {
            "request_id": request_id, "fy": fy_label(invoice_date),
            "invoice_date": invoice_date.isoformat(), "supply_date": supply_date.isoformat(),
            "supplier_name": SUPPLIER["name"], "supplier_gstin": SUPPLIER["gstin"],
            "supplier_address": SUPPLIER["address"],
            "customer_name": customer, "customer_address": str(d.get("customer_address", "")).strip()[:300],
            "customer_gstin": c_gstin, "customer_phone": str(d.get("customer_phone", "")).strip()[:20],
            "place_of_supply": STATES[pos_code], "place_of_supply_code": pos_code, "is_inter_state": inter,
            "price_mode": mode, "payment_fund": fund, "sync": sync,
            "existing_reference": "" if sync else str(d.get("existing_reference", "")).strip()[:100],
            "total_taxable": str(tot["t"]), "total_cgst": str(tot["c"]), "total_sgst": str(tot["s"]),
            "total_igst": str(tot["i"]), "total_amount": str(tot["a"]), "items": items,
        }
        try:
            res = supabase.rpc("issue_gst_invoice", {"p": payload}).execute().data
            res = res[0] if isinstance(res, list) else res
            return jsonify({"invoice": load_invoice(res["id"]), "duplicate": res.get("duplicate", False)}), 201
        except Exception as e:
            log.exception("issue_gst_invoice failed")
            m = re.search(r"Insufficient funds in ([^\"'\\}]+)", str(e))
            if m:
                return jsonify({"error": f"Insufficient funds in {m.group(1).strip()}. Nothing was saved."}), 400
            return jsonify({"error": "Could not issue invoice. Nothing was saved."}), 500
