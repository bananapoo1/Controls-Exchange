from pathlib import Path

path = Path("app.py")
text = path.read_text(encoding="utf-8")

old_import = "import logging\nimport os\n"
new_import = "import logging\nimport math\nimport os\n"
if old_import not in text:
    raise SystemExit("Expected logging/os import block not found")
text = text.replace(old_import, new_import, 1)

old_can_respond = '        can_respond=user["role"]=="supplier" and my_recipient is not None and not is_buyer_company and can(user,"trade") and user["email_verified"] and rfq["status"]=="open" and my_recipient["status"] not in {"accepted","not_selected"}\n'
new_can_respond = '        can_respond=user["role"]=="supplier" and my_recipient is not None and not is_buyer_company and can(user,"trade") and user["email_verified"] and user["company_verified"] and commercial_access(conn,user["company_id"]) and rfq["status"]=="open" and my_recipient["status"] not in {"accepted","not_selected"}\n'
if old_can_respond not in text:
    raise SystemExit("Expected can_respond guard not found")
text = text.replace(old_can_respond, new_can_respond, 1)

old_role_guard = '''    user=require_permission(request,"trade")
    if user["role"]!="supplier": raise HTTPException(status_code=403)
    status=str(form.get("status","quoted")); status=status if status in {"quoted","declined"} else "quoted"
'''
new_role_guard = '''    user=require_permission(request,"trade")
    if user["role"]!="supplier" or not user["company_verified"]: raise HTTPException(status_code=403)
    with db() as access_conn:
        if not commercial_access(access_conn,user["company_id"]):
            flash(request,"Supplier access is paused. Choose a plan in Billing before responding to RFQs.","error")
            return RedirectResponse("/billing",status_code=303)
    status=str(form.get("status","quoted")); status=status if status in {"quoted","declined"} else "quoted"
'''
if old_role_guard not in text:
    raise SystemExit("Expected RFQ role guard not found")
text = text.replace(old_role_guard, new_role_guard, 1)

old_price = '''    if price_raw:
        try: price=float(price_raw)
        except ValueError: flash(request,"Quote price must be a number.","error"); return RedirectResponse(f"/rfqs/{rfq_id}",status_code=303)
'''
new_price = '''    if price_raw:
        try:
            price=float(price_raw)
            if not math.isfinite(price) or price <= 0: raise ValueError
        except ValueError:
            flash(request,"Quote price must be a positive number.","error")
            return RedirectResponse(f"/rfqs/{rfq_id}",status_code=303)
'''
if old_price not in text:
    raise SystemExit("Expected quote price parsing block not found")
text = text.replace(old_price, new_price, 1)

path.write_text(text, encoding="utf-8")
print("Applied supplier quote validation/access guards")
